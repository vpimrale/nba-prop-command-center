"""Read-side queries: data status / job health, bookmaker coverage, closing lines,
price-at-time. These are what the dashboard's "Data status" panel will read (Stage 5).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Engine

from .db.schema import games, job_runs, odds_current, odds_snapshots, slot_completions, snapshot_runs, utcnow
from .pipeline.budget import credits_used_this_month, last_reported_remaining


def status_report(engine: Engine, now: datetime | None = None) -> dict:
    now = now or utcnow()
    with engine.connect() as conn:
        last_ok = conn.execute(select(snapshot_runs).where(snapshot_runs.c.status.in_(["ok", "partial"]))
                               .order_by(snapshot_runs.c.finished_at.desc()).limit(1)).mappings().first()
        last_any = conn.execute(select(snapshot_runs).order_by(snapshot_runs.c.id.desc()).limit(1)).mappings().first()
        last_events = conn.execute(select(job_runs).where(job_runs.c.job_name == "refresh_events")
                                   .order_by(job_runs.c.id.desc()).limit(1)).mappings().first()
        last_tick = conn.execute(select(job_runs).where(job_runs.c.job_name == "scheduler_tick")
                                 .order_by(job_runs.c.id.desc()).limit(1)).mappings().first()
        missed_24h = conn.execute(select(func.count()).select_from(slot_completions).where(and_(
            slot_completions.c.status == "missed", slot_completions.c.completed_at >= now - timedelta(hours=24)))).scalar()
        active_by_book = dict(conn.execute(select(odds_current.c.bookmaker, func.count()).where(and_(
            odds_current.c.market_status == "active", odds_current.c.commence_time > now)).group_by(odds_current.c.bookmaker)).all())
        upcoming = conn.execute(select(func.count()).select_from(games).where(games.c.commence_time > now)).scalar()
        total_rows = conn.execute(select(func.count()).select_from(odds_snapshots)).scalar()
        used = credits_used_this_month(conn, now)
        remaining = last_reported_remaining(conn)

    def age(dt):
        return None if not dt else round((now - dt).total_seconds() / 60, 1)

    return {
        "as_of": now.isoformat(),
        "last_successful_snapshot": None if not last_ok else {"finished_at": last_ok["finished_at"].isoformat(), "age_min": age(last_ok["finished_at"]),
                                                               "status": last_ok["status"], "reason": last_ok["reason"]},
        "last_snapshot_attempt": None if not last_any else {"status": last_any["status"], "started_at": last_any["started_at"].isoformat(),
                                                            "detail": last_any["detail"]},
        "last_events_refresh": None if not last_events else {"status": last_events["status"], "at": last_events["started_at"].isoformat(),
                                                             "detail": last_events["detail"]},
        "last_scheduler_tick": None if not last_tick else {"status": last_tick["status"], "at": last_tick["started_at"].isoformat(),
                                                           "age_min": age(last_tick["started_at"])},
        "missed_slots_24h": int(missed_24h or 0),
        "upcoming_games_known": int(upcoming or 0),
        "active_pregame_quotes_by_bookmaker": active_by_book,
        "snapshot_rows_total": int(total_rows or 0),
        "credits_used_this_month_reported": used,
        "credits_remaining_reported_by_provider": remaining,
    }


def coverage_report(engine: Engine, game_date_et: str) -> list[dict]:
    """Per bookmaker × market for one ET game date: games, players, quotes, alt lines.
    Counts what was actually observed (ever, for that date) — not what a provider claims."""
    q = (select(odds_snapshots.c.bookmaker, odds_snapshots.c.market_key,
                func.count(func.distinct(odds_snapshots.c.provider_event_id)).label("games"),
                func.count(func.distinct(odds_snapshots.c.player_name)).label("players"),
                func.count(func.distinct(odds_snapshots.c.quote_key)).label("distinct_quotes"))
         .where(and_(odds_snapshots.c.game_date_et == game_date_et, odds_snapshots.c.market_status == "active"))
         .group_by(odds_snapshots.c.bookmaker, odds_snapshots.c.market_key)
         .order_by(odds_snapshots.c.bookmaker, odds_snapshots.c.market_key))
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(q)]


def price_at(engine: Engine, quote_key: str, at: datetime) -> dict | None:
    """The quote as offered at time `at` (None if never seen by then). A 'removed' row
    means it was NOT on offer at `at`."""
    q = (select(odds_snapshots).where(and_(odds_snapshots.c.quote_key == quote_key, odds_snapshots.c.captured_at <= at))
         .order_by(odds_snapshots.c.captured_at.desc(), odds_snapshots.c.id.desc()).limit(1))
    with engine.connect() as conn:
        row = conn.execute(q).mappings().first()
        return dict(row) if row else None


def closing_lines(engine: Engine, provider_event_id: str) -> list[dict]:
    """Last observed pre-tip state of every quote for an event (the closing reference).
    `captured_at` shows how close to tip the last observation was — a closing line is
    only as good as the last snapshot before tip-off."""
    with engine.connect() as conn:
        commence = conn.execute(select(games.c.commence_time).where(games.c.provider_event_id == provider_event_id)).scalar()
        if commence is None:
            return []
        latest = (select(odds_snapshots.c.quote_key, func.max(odds_snapshots.c.id).label("max_id"))
                  .where(and_(odds_snapshots.c.provider_event_id == provider_event_id, odds_snapshots.c.captured_at <= commence))
                  .group_by(odds_snapshots.c.quote_key).subquery())
        rows = conn.execute(select(odds_snapshots).join(latest, odds_snapshots.c.id == latest.c.max_id)
                            .order_by(odds_snapshots.c.bookmaker, odds_snapshots.c.market_key, odds_snapshots.c.player_name,
                                      odds_snapshots.c.line, odds_snapshots.c.side)).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["offered_at_close"] = d["market_status"] == "active"
        d["minutes_before_tip"] = round((commence - d["captured_at"]).total_seconds() / 60, 1)
        out.append(d)
    return out
