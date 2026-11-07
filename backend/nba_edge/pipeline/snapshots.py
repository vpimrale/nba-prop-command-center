"""Odds snapshot capture: fetch -> normalize -> diff against current -> append changes.

Guarantees:
* Append-only history: rows in odds_snapshots are only ever INSERTED.
* No fabricated prices: every stored price came from a provider response in this run.
* A failed fetch never writes 'removed' tombstones (an outage is not a market change).
  Tombstones are written only for quotes of a bookmaker+market that was REQUESTED in a
  SUCCESSFUL fetch of that event and is absent from it.
* Only pre-game odds: events whose tip-off has passed are not fetched.
* Every run is recorded (incl. blocked/skipped/failed) with provider-reported credits.
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
from ..odds.client import AuthError, MissingApiKey, NotFound, OddsApiClient, OddsApiError, QuotaExceeded, Usage
from ..odds.normalize import PROVIDER, NormalizeResult, game_date_et, normalize_event_odds, parse_ts
from . import budget

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------------
# Request recording (provider calls -> api_requests). No key, no query string.
# ---------------------------------------------------------------------------------
class RequestRecorder:
    def __init__(self, engine: Engine, clock=utcnow):
        self.engine = engine
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
                run_id=self.run_id, requested_at=self.clock(), provider=PROVIDER, path=path, status_code=status,
                duration_ms=duration_ms, credits_last=usage.last if usage else None, credits_used=usage.used if usage else None,
                credits_remaining=usage.remaining if usage else None, error=error))


def make_client(settings: Settings, engine: Engine, *, transport=None, sleep=None, clock=utcnow) -> tuple[OddsApiClient, RequestRecorder]:
    rec = RequestRecorder(engine, clock)
    kwargs = {"transport": transport, "on_request": rec}
    if sleep is not None:
        kwargs["sleep"] = sleep
    return OddsApiClient(settings, **kwargs), rec


def _iso(dt: datetime) -> str:
    """Provider timestamp format: UTC, second precision, trailing Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------------
# Events (schedule as the odds provider sees it)
# ---------------------------------------------------------------------------------
def upsert_game(conn: Connection, *, event_id: str, sport_key: str, commence: datetime, home: str, away: str, now: datetime) -> None:
    existing = conn.execute(select(games.c.provider_event_id).where(and_(games.c.provider == PROVIDER, games.c.provider_event_id == event_id))).first()
    values = dict(sport_key=sport_key, commence_time=commence, game_date_et=game_date_et(commence), home_team=home, away_team=away, last_seen_at=now)
    if existing:
        conn.execute(update(games).where(and_(games.c.provider == PROVIDER, games.c.provider_event_id == event_id)).values(**values))
    else:
        conn.execute(insert(games).values(provider=PROVIDER, provider_event_id=event_id, first_seen_at=now, **values))


def _elapsed_since(now: datetime, t0: float) -> datetime:
    """Finish timestamp on the same clock as `now` (real or simulated): now + elapsed."""
    return now + timedelta(seconds=time.monotonic() - t0)


def refresh_events(engine: Engine, client: OddsApiClient, now: datetime | None = None, horizon_h: int = 36) -> dict:
    now = now or utcnow()
    t0 = time.monotonic()
    with engine.begin() as conn:
        job_id = conn.execute(insert(job_runs).values(job_name="refresh_events", started_at=now, status="running")).inserted_primary_key[0]
    status, detail, count = "ok", "", 0
    try:
        resp = client.list_events(_iso(now), _iso(now + timedelta(hours=horizon_h)))
        with engine.begin() as conn:
            for ev in resp.data or []:
                commence = parse_ts(ev.get("commence_time"))
                if not ev.get("id") or not commence:
                    continue
                upsert_game(conn, event_id=str(ev["id"]), sport_key=ev.get("sport_key", client.settings.sport_key), commence=commence,
                            home=ev.get("home_team", ""), away=ev.get("away_team", ""), now=now)
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
         "market_code", "is_alternate", "player_name", "side", "line"]


def _quote_values(q) -> dict:
    return dict(quote_key=q.key, provider=q.provider, sport_key=q.sport_key, provider_event_id=q.event_id, commence_time=q.commence_time,
                game_date_et=q.game_date_et, home_team=q.home_team, away_team=q.away_team, bookmaker=q.bookmaker, market_key=q.market_key,
                market_code=q.market_code, is_alternate=q.is_alternate, player_name=q.player_name, player_team=None, opponent=None,
                side=q.side, line=q.line, american_odds=q.american_odds, decimal_odds=q.decimal_odds, market_status="active")


@dataclass
class EventWriteResult:
    inserted: int = 0
    removed: int = 0
    unchanged: int = 0
    change_counts: dict = field(default_factory=dict)


def apply_event_quotes(conn: Connection, *, run_id: int, now: datetime, reason: str, event_id: str, result: NormalizeResult,
                       requested_books: tuple[str, ...], requested_markets: tuple[str, ...]) -> EventWriteResult:
    out = EventWriteResult()
    current = {r.quote_key: r for r in conn.execute(select(odds_current).where(and_(
        odds_current.c.provider == PROVIDER, odds_current.c.provider_event_id == event_id)))}
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
            touch.append({"k": q.key, "last_seen_at": now, "last_run_id": run_id})
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
    markets_scope = set(requested_markets)
    for key, cur in current.items():
        if key in seen or cur.market_status != "active":
            continue
        if books_scope is not None and cur.bookmaker not in books_scope:
            continue
        if cur.market_key not in markets_scope:
            continue
        row = {c: getattr(cur, c) for c in _COPY}
        snap_rows.append({**row, "quote_key": key, "provider_event_id": event_id, "player_team": cur.player_team, "opponent": cur.opponent,
                          "american_odds": None, "decimal_odds": None, "market_status": "removed", "run_id": run_id, "captured_at": now,
                          "snapshot_reason": reason, "change_type": "removed", "prev_american_odds": cur.american_odds,
                          "book_last_update": None, "market_last_update": None, "model_projection": None, "model_version": None})
        cur_updates.append({**{f"v_{c}": getattr(cur, c) for c in _COPY}, "v_provider_event_id": event_id, "v_player_team": cur.player_team,
                            "v_opponent": cur.opponent, "v_american_odds": cur.american_odds, "v_decimal_odds": cur.decimal_odds,
                            "v_market_status": "removed", "k": key, "v_last_seen_at": cur.last_seen_at, "v_last_changed_at": now,
                            "v_last_run_id": run_id})
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
        conn.execute(update(odds_current).where(odds_current.c.quote_key == bindparam("k"))
                     .values(last_seen_at=bindparam("last_seen_at"), last_run_id=bindparam("last_run_id")), touch)
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


def upcoming_event_ids(engine: Engine, now: datetime, *, game_date: str | None = None, event_ids: list[str] | None = None) -> list[str]:
    q = select(games.c.provider_event_id).where(and_(games.c.provider == PROVIDER, games.c.commence_time > now)).order_by(games.c.commence_time)
    if game_date:
        q = q.where(games.c.game_date_et == game_date)
    if event_ids is not None:
        q = q.where(games.c.provider_event_id.in_(event_ids))
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(q)]


def run_snapshot(engine: Engine, settings: Settings, client: OddsApiClient, recorder: RequestRecorder, *, event_ids: list[str],
                 reason: str = "manual", slot_key: str | None = None, now: datetime | None = None) -> dict:
    now = now or utcnow()
    started = (now, time.monotonic())
    with engine.begin() as conn:
        run_id = conn.execute(insert(snapshot_runs).values(slot_key=slot_key, reason=reason, started_at=now, status="running",
                                                           events_planned=len(event_ids))).inserted_primary_key[0]
    recorder.run_id = run_id
    recorder.reported, recorder.remaining = 0, None
    if not settings.odds_api_key:
        return _finish_run(engine, run_id, started=started, status="blocked_no_key", detail="ODDS_API_KEY is not set — no provider call was made.")
    # Pre-game only: never spend credits on (or store) a game known to have tipped off.
    with engine.connect() as conn:
        tipped = {r[0] for r in conn.execute(select(games.c.provider_event_id).where(and_(
            games.c.provider == PROVIDER, games.c.provider_event_id.in_(event_ids), games.c.commence_time <= now)))} if event_ids else set()
    event_ids = [e for e in event_ids if e not in tipped]
    if not event_ids:
        detail = "No upcoming (pre-tip) events to snapshot." + (f" Skipped {len(tipped)} already-started game(s)." if tipped else "")
        return _finish_run(engine, run_id, started=started, status="no_events", detail=detail)
    with engine.connect() as conn:
        decision = budget.decide(settings, conn, len(event_ids), now)
    if decision.events_allowed == 0:
        return _finish_run(engine, run_id, started=started, status="skipped_budget", credits_estimated=0, detail=decision.reason)
    todo = event_ids[: decision.events_allowed]
    notes = [] if decision.events_allowed >= len(event_ids) else [f"budget: {decision.reason}; fetched {len(todo)}/{len(event_ids)}"]
    fetched = failed = quotes = inserted = removed = skipped = 0
    books: set[str] = set()
    fatal = None
    for ev_id in todo:
        try:
            resp = client.event_odds(ev_id, settings.markets)
        except (AuthError, QuotaExceeded, MissingApiKey) as e:
            fatal = e
            break
        except NotFound as e:
            failed += 1
            notes.append(f"{ev_id}: not found ({e})")
            continue
        except OddsApiError as e:
            failed += 1
            notes.append(f"{ev_id}: {e}")
            continue
        payload = resp.data or {}
        if str(payload.get("id", ev_id)) != ev_id:
            failed += 1
            notes.append(f"{ev_id}: response was for a different event ({payload.get('id')}) — ignored")
            continue
        result = normalize_event_odds({**payload, "id": ev_id})
        commence = parse_ts(payload.get("commence_time"))
        if commence is not None and commence <= now:
            # The provider says this game has already started (e.g. tip-off moved up):
            # live odds are out of scope — record the game time, store no prices.
            with engine.begin() as conn:
                upsert_game(conn, event_id=ev_id, sport_key=payload.get("sport_key", settings.sport_key), commence=commence,
                            home=payload.get("home_team", ""), away=payload.get("away_team", ""), now=now)
            notes.append(f"{ev_id}: already started per provider ({commence.isoformat()}) — prices not stored")
            continue
        with engine.begin() as conn:
            if commence:
                upsert_game(conn, event_id=ev_id, sport_key=payload.get("sport_key", settings.sport_key), commence=commence,
                            home=payload.get("home_team", ""), away=payload.get("away_team", ""), now=now)
            w = apply_event_quotes(conn, run_id=run_id, now=now, reason=reason, event_id=ev_id, result=result,
                                   requested_books=settings.bookmakers, requested_markets=settings.markets)
        fetched += 1
        quotes += len(result.quotes)
        inserted += w.inserted
        removed += w.removed
        skipped += result.skipped
        books |= result.bookmakers_seen
    if fatal is not None:
        status = "blocked_auth" if isinstance(fatal, AuthError) else ("partial" if fetched else "failed")
        notes.insert(0, f"stopped: {type(fatal).__name__}: {fatal}")
    elif failed and fetched:
        status = "partial"
    elif failed and not fetched:
        status = "failed"
    else:
        status = "ok"
    missing_books = [b for b in settings.bookmakers if b not in books] if fetched else []
    if missing_books:
        notes.append(f"requested bookmakers absent from every response: {', '.join(missing_books)}")
    return _finish_run(engine, run_id, started=started, status=status, events_fetched=fetched, events_failed=failed, quotes_seen=quotes,
                       rows_inserted=inserted, rows_removed=removed, outcomes_skipped=skipped,
                       credits_estimated=decision.per_event_estimate * len(todo), credits_reported=recorder.reported,
                       credits_remaining=recorder.remaining, bookmakers_seen=",".join(sorted(books)) or None,
                       detail="; ".join(notes) or None)
