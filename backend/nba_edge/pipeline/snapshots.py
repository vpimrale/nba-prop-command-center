"""Odds snapshot capture: provider fetch -> normalize -> diff against current -> append changes.

Provider-agnostic: everything source-specific lives in nba_edge/providers/*.

Guarantees (all covered by tests):
* Append-only history: rows in odds_snapshots are only ever INSERTED.
* No fabricated prices: every stored price came from a provider response in this run.
* A failed fetch never writes 'removed' tombstones (an outage is not a market change).
  Tombstones are written only for quotes of a bookmaker+market that was REQUESTED from
  that provider in a SUCCESSFUL fetch of that event and are absent from it.
* Only pre-game odds: events that have tipped off (or that the provider marks live) are
  never stored.
* Stale data is never presented as current: quotes from a bookmaker feed older than
  `stale_after_min` are stored with stale=true and counted in the run record.
* Every run is recorded (incl. blocked/skipped/failed) with provider-reported usage.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, bindparam, insert, select, update
from sqlalchemy.engine import Connection, Engine

from ..config import Settings
from ..db.schema import api_requests, games, job_runs, odds_current, odds_snapshots, snapshot_runs, utcnow
from ..odds.client import AuthError, MissingApiKey, OddsApiError, QuotaExceeded, Usage
from ..odds.normalize import NormalizeResult, game_date_et
from ..providers import OddsProvider, make_provider
from . import budget

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------------
# Request recording (provider calls -> api_requests). No key, no query string.
# ---------------------------------------------------------------------------------
class RequestRecorder:
    def __init__(self, engine: Engine, provider_name: str, clock=utcnow):
        self.engine = engine
        self.provider_name = provider_name
        self.run_id: int | None = None
        self.clock = clock
        self.reported = 0
        self.remaining: int | None = None

    def __call__(self, path: str, status: int | None, usage: Usage | None, duration_ms: int, error: str | None) -> None:
        if usage and usage.last is not None:
            self.reported += usage.last
        if usage and usage.remaining is not None:
            self.remaining = usage.remaining
        with self.engine.begin() as conn:
            conn.execute(insert(api_requests).values(
                run_id=self.run_id, requested_at=self.clock(), provider=self.provider_name, path=path, status_code=status,
                duration_ms=duration_ms, credits_last=usage.last if usage else None, credits_used=usage.used if usage else None,
                credits_remaining=usage.remaining if usage else None, error=error))


def open_provider(settings: Settings, engine: Engine, *, transport=None, sleep=None, clock=utcnow) -> tuple[OddsProvider, RequestRecorder]:
    rec = RequestRecorder(engine, settings.odds_provider, clock)
    return make_provider(settings, recorder=rec, transport=transport, sleep=sleep, clock=clock), rec


make_client = open_provider  # older name


def _elapsed_since(now: datetime, t0: float) -> datetime:
    """Finish timestamp on the same clock as `now` (real or simulated): now + elapsed."""
    return now + timedelta(seconds=time.monotonic() - t0)


# ---------------------------------------------------------------------------------
# Events (schedule as the odds provider sees it)
# ---------------------------------------------------------------------------------
def upsert_game(conn: Connection, *, provider: str, event_id: str, sport_key: str, commence: datetime, home: str, away: str,
                now: datetime, espn_event_id: str | None = None) -> None:
    where = and_(games.c.provider == provider, games.c.provider_event_id == event_id)
    existing = conn.execute(select(games.c.provider_event_id).where(where)).first()
    values = dict(sport_key=sport_key, commence_time=commence, game_date_et=game_date_et(commence), home_team=home, away_team=away, last_seen_at=now)
    if espn_event_id:
        values["espn_event_id"] = espn_event_id   # never overwrite a known id with "unknown"
    if existing:
        conn.execute(update(games).where(where).values(**values))
    else:
        conn.execute(insert(games).values(provider=provider, provider_event_id=event_id, first_seen_at=now, **values))


def refresh_events(engine: Engine, provider: OddsProvider, now: datetime | None = None, horizon_h: int = 36) -> dict:
    now = now or utcnow()
    t0 = time.monotonic()
    with engine.begin() as conn:
        job_id = conn.execute(insert(job_runs).values(job_name=f"refresh_events:{provider.name}", started_at=now, status="running")).inserted_primary_key[0]
    status, detail, count = "ok", "", 0
    try:
        if not provider.has_credentials():
            raise MissingApiKey(f"no API key configured for {provider.name}")
        events = provider.list_events(now, now + timedelta(hours=horizon_h))
        with engine.begin() as conn:
            for ev in events:
                upsert_game(conn, provider=provider.name, event_id=ev.event_id, sport_key=ev.sport_key, commence=ev.commence,
                            home=ev.home, away=ev.away, now=now, espn_event_id=ev.espn_event_id)
                count += 1
        detail = f"{count} events in next {horizon_h}h"
    except MissingApiKey as e:
        status, detail = "blocked_no_key", str(e)
    except AuthError as e:
        status, detail = "blocked_auth", str(e)
    except OddsApiError as e:
        status, detail = "failed", str(e)
    with engine.begin() as conn:
        conn.execute(update(job_runs).where(job_runs.c.id == job_id).values(finished_at=_elapsed_since(now, t0), status=status, detail=detail))
    return {"status": status, "events": count, "detail": detail}


# ---------------------------------------------------------------------------------
# Diff + write for one event
# ---------------------------------------------------------------------------------
_COPY = ["provider", "sport_key", "commence_time", "game_date_et", "home_team", "away_team", "bookmaker", "market_key",
         "market_code", "is_alternate", "player_name", "side", "line", "provider_player_id", "line_type", "outcome_label"]


def _quote_values(q) -> dict:
    return dict(quote_key=q.key, provider=q.provider, sport_key=q.sport_key, provider_event_id=q.event_id, commence_time=q.commence_time,
                game_date_et=q.game_date_et, home_team=q.home_team, away_team=q.away_team, bookmaker=q.bookmaker, market_key=q.market_key,
                market_code=q.market_code, is_alternate=q.is_alternate, player_name=q.player_name, player_team=None, opponent=None,
                side=q.side, line=q.line, american_odds=q.american_odds, decimal_odds=q.decimal_odds, market_status="active",
                provider_player_id=q.provider_player_id, line_type=q.line_type, outcome_label=q.outcome_label,
                source_seen_at=q.source_seen_at, source_changed_at=q.source_changed_at, stale=q.stale)


@dataclass
class EventWriteResult:
    inserted: int = 0
    removed: int = 0
    unchanged: int = 0
    change_counts: dict = field(default_factory=dict)


def apply_event_quotes(conn: Connection, *, run_id: int, now: datetime, reason: str, event_id: str, result: NormalizeResult,
                       provider_name: str, requested_books: tuple[str, ...], market_in_scope) -> EventWriteResult:
    out = EventWriteResult()
    current = {r.quote_key: r for r in conn.execute(select(odds_current).where(and_(
        odds_current.c.provider == provider_name, odds_current.c.provider_event_id == event_id)))}
    snap_rows, cur_inserts, cur_updates, touch = [], [], [], []
    seen = set()
    for q in result.quotes:
        seen.add(q.key)
        cur = current.get(q.key)
        if cur is None:
            change = "new"
        elif cur.market_status != "active":
            change = "reappeared"
        elif cur.american_odds != q.american_odds:
            change = "price_change"
        else:
            change = None
        if change is None:
            out.unchanged += 1
            touch.append({"k": q.key, "v_last_seen_at": now, "v_last_run_id": run_id, "v_stale": q.stale,
                          "v_source_seen_at": q.source_seen_at, "v_source_changed_at": q.source_changed_at})
            continue
        vals = _quote_values(q)
        snap_rows.append({**vals, "run_id": run_id, "captured_at": now, "snapshot_reason": reason, "change_type": change,
                          "prev_american_odds": cur.american_odds if cur is not None else None,
                          "book_last_update": q.book_last_update, "market_last_update": q.market_last_update,
                          "model_projection": None, "model_version": None})
        out.change_counts[change] = out.change_counts.get(change, 0) + 1
        if cur is None:
            cur_inserts.append({**vals, "first_seen_at": now, "last_seen_at": now, "last_changed_at": now, "last_run_id": run_id})
        else:
            cur_updates.append({**{f"v_{k}": v for k, v in vals.items() if k != "quote_key"}, "k": q.key,
                                "v_last_seen_at": now, "v_last_changed_at": now, "v_last_run_id": run_id})
    # Tombstones: active quotes of a requested book+market that this successful fetch no longer contains.
    books_scope = set(requested_books) if requested_books else None
    for key, cur in current.items():
        if key in seen or cur.market_status != "active":
            continue
        if books_scope is not None and cur.bookmaker not in books_scope:
            continue
        if not market_in_scope(cur.market_key):
            continue
        row = {c: getattr(cur, c) for c in _COPY}
        snap_rows.append({**row, "quote_key": key, "provider_event_id": event_id, "player_team": cur.player_team, "opponent": cur.opponent,
                          "american_odds": None, "decimal_odds": None, "market_status": "removed", "run_id": run_id, "captured_at": now,
                          "snapshot_reason": reason, "change_type": "removed", "prev_american_odds": cur.american_odds,
                          "book_last_update": None, "market_last_update": None, "model_projection": None, "model_version": None,
                          "source_seen_at": cur.source_seen_at, "source_changed_at": cur.source_changed_at, "stale": None})
        cur_updates.append({**{f"v_{c}": getattr(cur, c) for c in _COPY}, "v_provider_event_id": event_id, "v_player_team": cur.player_team,
                            "v_opponent": cur.opponent, "v_american_odds": cur.american_odds, "v_decimal_odds": cur.decimal_odds,
                            "v_market_status": "removed", "k": key, "v_last_seen_at": cur.last_seen_at, "v_last_changed_at": now,
                            "v_last_run_id": run_id, "v_source_seen_at": cur.source_seen_at, "v_source_changed_at": cur.source_changed_at,
                            "v_stale": None})
        out.removed += 1
        out.change_counts["removed"] = out.change_counts.get("removed", 0) + 1
    if snap_rows:
        conn.execute(insert(odds_snapshots), snap_rows)
    if cur_inserts:
        conn.execute(insert(odds_current), cur_inserts)
    if cur_updates:
        cols = [c.name for c in odds_current.columns if c.name not in ("quote_key", "first_seen_at")]
        stmt = update(odds_current).where(odds_current.c.quote_key == bindparam("k")).values({c: bindparam(f"v_{c}") for c in cols})
        conn.execute(stmt, cur_updates)
    if touch:
        tcols = ["last_seen_at", "last_run_id", "stale", "source_seen_at", "source_changed_at"]
        conn.execute(update(odds_current).where(odds_current.c.quote_key == bindparam("k"))
                     .values({c: bindparam(f"v_{c}") for c in tcols}), touch)
    out.inserted = len(snap_rows) - out.removed
    return out


# ---------------------------------------------------------------------------------
# A full snapshot run
# ---------------------------------------------------------------------------------
def _finish_run(engine: Engine, run_id: int, *, started: tuple[datetime, float], **values) -> dict:
    values.setdefault("finished_at", _elapsed_since(*started))
    with engine.begin() as conn:
        conn.execute(update(snapshot_runs).where(snapshot_runs.c.id == run_id).values(**values))
        row = conn.execute(select(snapshot_runs).where(snapshot_runs.c.id == run_id)).mappings().one()
    return dict(row)


def upcoming_event_ids(engine: Engine, now: datetime, *, provider: str, game_date: str | None = None,
                       event_ids: list[str] | None = None) -> list[str]:
    q = (select(games.c.provider_event_id).where(and_(games.c.provider == provider, games.c.commence_time > now))
         .order_by(games.c.commence_time, games.c.provider_event_id))
    if game_date:
        q = q.where(games.c.game_date_et == game_date)
    if event_ids is not None:
        q = q.where(games.c.provider_event_id.in_(event_ids))
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(q)]


def run_snapshot(engine: Engine, settings: Settings, provider: OddsProvider, recorder: RequestRecorder, *, event_ids: list[str],
                 reason: str = "manual", slot_key: str | None = None, now: datetime | None = None) -> dict:
    now = now or utcnow()
    started = (now, time.monotonic())
    pname = provider.name
    with engine.begin() as conn:
        run_id = conn.execute(insert(snapshot_runs).values(slot_key=slot_key, reason=reason, started_at=now, status="running",
                                                           provider=pname, events_planned=len(event_ids))).inserted_primary_key[0]
    recorder.run_id = run_id
    recorder.reported, recorder.remaining = 0, None
    if not provider.has_credentials():
        return _finish_run(engine, run_id, started=started, status="blocked_no_key",
                           detail=f"No API key configured for {pname} ({provider.key_env_var} is not set) — no provider call was made.")
    # Pre-game only: never spend requests on (or store) a game known to have tipped off.
    with engine.connect() as conn:
        tipped = {r[0] for r in conn.execute(select(games.c.provider_event_id).where(and_(
            games.c.provider == pname, games.c.provider_event_id.in_(event_ids), games.c.commence_time <= now)))} if event_ids else set()
    event_ids = [e for e in event_ids if e not in tipped]
    if not event_ids:
        detail = "No upcoming (pre-tip) events to snapshot." + (f" Skipped {len(tipped)} already-started game(s)." if tipped else "")
        return _finish_run(engine, run_id, started=started, status="no_events", detail=detail)
    with engine.connect() as conn:
        decision = budget.decide(settings, conn, provider, len(event_ids), now)
    if decision.units_allowed < max(1, provider.estimate_units(1)):
        return _finish_run(engine, run_id, started=started, status="skipped_budget", credits_estimated=0,
                           detail=f"budget: {decision.limit} unit(s) left — not enough for one event ({provider.estimate_units(1)})")
    notes: list[str] = [] if decision.reason == "ok" else [f"budget: {decision.reason}"]
    fatal = None
    try:
        outcome = provider.fetch_odds(event_ids, max_units=decision.units_allowed)
    except (AuthError, QuotaExceeded, MissingApiKey) as e:
        fatal, outcome = e, None
    fetched = quotes = inserted = removed = skipped = stale = 0
    excluded: dict[str, int] = {}
    books: set[str] = set()
    failed = 0
    if outcome is not None:
        notes.extend(outcome.notes)
        for ev_id, why in outcome.failed.items():
            failed += 1
            notes.append(f"{ev_id}: {why}")
        if outcome.not_fetched:
            notes.append(f"not fetched (budget): {len(outcome.not_fetched)} event(s)")
            failed += len(outcome.not_fetched)
        for eo in outcome.odds:
            ev = eo.event
            started_already = ev.live or (ev.commence is not None and ev.commence <= now)
            with engine.begin() as conn:
                if ev.commence is not None:
                    upsert_game(conn, provider=pname, event_id=ev.event_id, sport_key=ev.sport_key, commence=ev.commence,
                                home=ev.home, away=ev.away, now=now, espn_event_id=ev.espn_event_id)
                if started_already:
                    notes.append(f"{ev.event_id}: already started per provider — prices not stored")
                    continue
                w = apply_event_quotes(conn, run_id=run_id, now=now, reason=reason, event_id=ev.event_id, result=eo.result,
                                       provider_name=pname, requested_books=provider.requested_bookmakers,
                                       market_in_scope=provider.market_in_scope)
            fetched += 1
            quotes += len(eo.result.quotes)
            inserted += w.inserted
            removed += w.removed
            skipped += eo.result.skipped
            stale += eo.result.stale
            books |= eo.result.bookmakers_seen
            for k, v in (eo.result.excluded or {}).items():
                excluded[k] = excluded.get(k, 0) + v
    if fatal is not None:
        status = "blocked_auth" if isinstance(fatal, AuthError) else ("quota_exhausted" if isinstance(fatal, QuotaExceeded) else "failed")
        notes.insert(0, f"stopped: {type(fatal).__name__}: {fatal}")
    elif outcome is not None and not fetched and outcome.not_fetched and len(outcome.not_fetched) == failed:
        status = "skipped_budget"
    elif failed and fetched:
        status = "partial"
    elif failed and not fetched:
        status = "failed"
    else:
        status = "ok"
    missing_books = [b for b in provider.requested_bookmakers if b not in books] if fetched else []
    if missing_books:
        notes.append(f"requested bookmakers absent from every response: {', '.join(missing_books)}")
    if stale:
        notes.append(f"{stale} quote(s) flagged stale (bookmaker feed older than {settings.stale_after_min} min)")
    if excluded:
        notes.append("excluded: " + ", ".join(f"{k}={v}" for k, v in sorted(excluded.items())))
    return _finish_run(engine, run_id, started=started, status=status, events_fetched=fetched, events_failed=failed, quotes_seen=quotes,
                       rows_inserted=inserted, rows_removed=removed, outcomes_skipped=skipped, quotes_stale=stale,
                       credits_estimated=decision.estimate, credits_reported=recorder.reported,
                       credits_remaining=recorder.remaining, bookmakers_seen=",".join(sorted(books)) or None,
                       detail="; ".join(notes) or None)
