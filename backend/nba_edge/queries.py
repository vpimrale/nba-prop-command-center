"""Read-side queries: data status / job health, bookmaker coverage, closing lines,
price-at-time. These are what the dashboard's "Data status" panel will read (Stage 5).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, select
from sqlalchemy.engine import Engine

from .db.schema import api_requests, games, job_runs, odds_current, odds_snapshots, slot_completions, snapshot_runs, utcnow
from .pipeline.budget import credits_used_this_month, day_start, last_reported_remaining, units_used


def status_report(engine: Engine, now: datetime | None = None) -> dict:
    now = now or utcnow()
    with engine.connect() as conn:
        last_ok = conn.execute(select(snapshot_runs).where(snapshot_runs.c.status.in_(["ok", "partial"]))
                               .order_by(snapshot_runs.c.finished_at.desc()).limit(1)).mappings().first()
        last_any = conn.execute(select(snapshot_runs).order_by(snapshot_runs.c.id.desc()).limit(1)).mappings().first()
        last_events = conn.execute(select(job_runs).where(job_runs.c.job_name.like("refresh_events%"))
                                   .order_by(job_runs.c.id.desc()).limit(1)).mappings().first()
        last_tick = conn.execute(select(job_runs).where(job_runs.c.job_name == "scheduler_tick")
                                 .order_by(job_runs.c.id.desc()).limit(1)).mappings().first()
        missed_24h = conn.execute(select(func.count()).select_from(slot_completions).where(and_(
            slot_completions.c.status == "missed", slot_completions.c.completed_at >= now - timedelta(hours=24)))).scalar()
        active_by_book = dict(conn.execute(select(odds_current.c.bookmaker, func.count()).where(and_(
            odds_current.c.market_status == "active", odds_current.c.commence_time > now)).group_by(odds_current.c.bookmaker)).all())
        upcoming = conn.execute(select(func.count()).select_from(games).where(games.c.commence_time > now)).scalar()
        stale_active = conn.execute(select(func.count()).select_from(odds_current).where(and_(
            odds_current.c.market_status == "active", odds_current.c.commence_time > now, odds_current.c.stale.is_(True)))).scalar()
        newest_seen = dict(conn.execute(select(odds_current.c.bookmaker, func.max(odds_current.c.last_seen_at)).where(and_(
            odds_current.c.market_status == "active", odds_current.c.commence_time > now)).group_by(odds_current.c.bookmaker)).all())
        providers = [p for (p,) in conn.execute(select(api_requests.c.provider).distinct())]
        usage_by_provider = {p: {"requests_today_utc": units_used(conn, now, provider=p, window="day"),
                                 "units_this_month": credits_used_this_month(conn, now, provider=p),
                                 "provider_reported_remaining_today": last_reported_remaining(conn, p, since=day_start(now))}
                             for p in providers}
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
        "active_pregame_quotes_flagged_stale": int(stale_active or 0),
        "last_seen_age_min_by_bookmaker": {b: age(t) for b, t in newest_seen.items()},
        "usage_by_provider": usage_by_provider,
        "snapshot_rows_total": int(total_rows or 0),
        "credits_used_this_month_reported": used,
        "credits_remaining_reported_by_provider": remaining,
    }


def coverage_report(engine: Engine, game_date_et: str) -> list[dict]:
    """Per provider × bookmaker × market for one ET game date: games, players, main vs
    alternate quotes, stale quotes. Counts what was actually OBSERVED in stored snapshots
    (ever, for that date) — not what a provider claims to cover."""
    c = odds_snapshots.c
    q = (select(c.provider, c.bookmaker, c.market_key,
                func.count(func.distinct(c.provider_event_id)).label("games"),
                func.count(func.distinct(c.player_name)).label("players"),
                func.count(func.distinct(c.quote_key)).label("distinct_quotes"),
                func.sum(case((c.stale.is_(True), 1), else_=0)).label("stale_rows"))
         .where(and_(c.game_date_et == game_date_et, c.market_status == "active"))
         .group_by(c.provider, c.bookmaker, c.market_key).order_by(c.provider, c.bookmaker, c.market_key))
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(q)]


def verify_report(engine: Engine, game_date_et: str, books: tuple[str, ...] = ("fanduel", "fanatics"), sample: int = 6) -> dict:
    """Sanity checks over the CURRENT state of stored quotes for a date — the evidence that
    a source's player/market/line/side/book/odds fields are internally consistent."""
    from statistics import median

    from .odds.math import american_to_decimal
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(odds_current).where(and_(
            odds_current.c.game_date_et == game_date_et, odds_current.c.market_status == "active",
            odds_current.c.bookmaker.in_(books))))]
    out: dict = {"game_date_et": game_date_et, "books": {}}
    main_lines: dict = {}
    for b in books:
        br = [r for r in rows if r["bookmaker"] == b]
        bad_price = [r for r in br if r["american_odds"] is None or -100 < r["american_odds"] < 100]
        mains = [r for r in br if not r["is_alternate"]]
        pairs: dict = {}
        for r in mains:
            pairs.setdefault((r["provider_event_id"], r["market_key"], r["player_name"], r["line"]), {})[r["side"]] = r["american_odds"]
        two_sided = {k: v for k, v in pairs.items() if "over" in v and "under" in v}
        overround = [1 / american_to_decimal(v["over"]) + 1 / american_to_decimal(v["under"]) for v in two_sided.values()]
        for (ev, mk, pl, line), v in two_sided.items():
            main_lines.setdefault((ev, mk, pl), {})[b] = line
        out["books"][b] = {
            "games": len({r["provider_event_id"] for r in br}), "players": len({r["player_name"] for r in br}),
            "quotes": len(br), "main_quotes": len(mains), "alternate_quotes": len(br) - len(mains),
            "markets": sorted({r["market_key"] for r in br}),
            "main_lines_two_sided": len(two_sided), "main_lines_one_sided": len(pairs) - len(two_sided),
            "invalid_prices": len(bad_price), "stale_quotes": sum(1 for r in br if r.get("stale")),
            "with_provider_player_id": sum(1 for r in br if r.get("provider_player_id")),
            "overround_main_min_median_max": [round(min(overround), 4), round(median(overround), 4), round(max(overround), 4)] if overround else None,
            "sample": [{k: r[k] for k in ("player_name", "market_key", "side", "line", "american_odds", "outcome_label", "home_team", "away_team")}
                       for r in sorted(mains, key=lambda r: (r["player_name"], r["market_key"], r["side"]))[:sample]],
        }
    both = [v for v in main_lines.values() if len(v) >= 2]
    same = sum(1 for v in both if len(set(v.values())) == 1)
    out["cross_book_main_line"] = {"player_markets_on_all_books": len(both), "identical_line": same,
                                   "identical_pct": round(100 * same / len(both), 1) if both else None}
    return out


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
