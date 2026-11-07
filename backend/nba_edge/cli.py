"""Command line entry point: `python -m nba_edge <command>` (or `nba-edge <command>`).

Commands never print secrets. Commands that would call the paid provider refuse to do so
without ODDS_API_KEY and say so plainly ("BLOCKED"), exiting non-zero.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import Settings
from .db.engine import init_db, make_engine
from .logging_setup import configure_logging, register_secret
from .pipeline import budget
from .pipeline.schedule import run_due
from .pipeline.snapshots import open_provider, refresh_events, run_snapshot, upcoming_event_ids
from .queries import closing_lines, coverage_report, status_report, verify_report

log = logging.getLogger("nba_edge")
ET = ZoneInfo("America/New_York")


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _engine(settings: Settings):
    engine = make_engine(settings.database_url.reveal())
    init_db(engine)
    return engine


def cmd_config(settings, args) -> int:
    _print(settings.describe())
    return 0


def cmd_init_db(settings, args) -> int:
    version = init_db(make_engine(settings.database_url.reveal()))
    _print({"schema_version": version, "database": settings.describe()["database_url"]})
    return 0


def cmd_refresh_events(settings, args) -> int:
    engine = _engine(settings)
    client, _ = open_provider(settings, engine)
    with client:
        result = refresh_events(engine, client)
    _print(result)
    return 0 if result["status"] == "ok" else 2


def cmd_snapshot(settings, args) -> int:
    engine = _engine(settings)
    now = datetime.now(timezone.utc)
    client, recorder = open_provider(settings, engine)
    if not client.has_credentials():
        key_var = "PROPLINE_API_KEY" if client.name == "propline" else "ODDS_API_KEY"
        print(f"BLOCKED: {key_var} is not set (provider {client.name}). No provider call was made.", file=sys.stderr)
    with client:
        if client.has_credentials() and not args.event:
            ev = refresh_events(engine, client, now)
            if ev["status"] != "ok":
                _print({"events_refresh": ev})
                return 2
        day = args.date or now.astimezone(ET).date().isoformat()
        ids = upcoming_event_ids(engine, now, provider=client.name, game_date=None if args.event else day, event_ids=args.event or None)
        result = run_snapshot(engine, settings, client, recorder, event_ids=ids, reason=args.reason, now=now)
    _print(result)
    return 0 if result["status"] in ("ok", "no_events") else 2


def cmd_run_due(settings, args) -> int:
    summary = run_due(_engine(settings), settings)
    _print(summary)
    return 0 if summary.get("status") == "ok" else 2


def cmd_run_scheduler(settings, args) -> int:
    engine = _engine(settings)
    log.info("scheduler loop: tick every %ss (Ctrl-C to stop)", args.interval)
    while True:
        try:
            s = run_due(engine, settings)
            log.info("tick: status=%s runs=%d missed=%d", s.get("status"), len(s["runs"]), len(s["missed"]))
        except Exception:  # keep the loop alive; the failure is logged and visible in job_runs
            log.exception("scheduler tick failed")
        if args.once:
            return 0
        time.sleep(args.interval)


def cmd_status(settings, args) -> int:
    _print(status_report(_engine(settings)))
    return 0


def cmd_coverage(settings, args) -> int:
    day = args.date or datetime.now(timezone.utc).astimezone(ET).date().isoformat()
    rows = coverage_report(_engine(settings), day)
    _print({"game_date_et": day, "rows": rows, "note": "observed in stored snapshots only" if rows else "no snapshots stored for this date"})
    return 0


def cmd_verify(settings, args) -> int:
    day = args.date or datetime.now(timezone.utc).astimezone(ET).date().isoformat()
    _print(verify_report(_engine(settings), day, tuple(args.book or ("fanduel", "fanatics"))))
    return 0


def cmd_closing(settings, args) -> int:
    _print(closing_lines(_engine(settings), args.event))
    return 0


def cmd_budget(settings, args) -> int:
    if settings.odds_provider == "propline":
        _print(budget.estimate_day_requests(settings, args.games_per_day))
    else:
        _print(budget.estimate_month(settings, args.games_per_day, args.snapshots_per_game, args.days))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nba-edge", description="NBA Quant Edge backend: odds snapshots (default source: PropLine free tier)")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("config", help="print effective configuration (no secrets)").set_defaults(fn=cmd_config)
    sub.add_parser("init-db", help="create/upgrade the database schema").set_defaults(fn=cmd_init_db)
    sub.add_parser("refresh-events", help="fetch upcoming NBA events from the odds provider").set_defaults(fn=cmd_refresh_events)
    s = sub.add_parser("snapshot", help="capture odds now for today's (ET) upcoming games, or specific events")
    s.add_argument("--event", action="append", help="provider event id (repeatable)")
    s.add_argument("--date", help="ET game date YYYY-MM-DD (default: today ET)")
    s.add_argument("--reason", default="manual")
    s.set_defaults(fn=cmd_snapshot)
    sub.add_parser("run-due", help="one scheduler tick: serve any due snapshot slots").set_defaults(fn=cmd_run_due)
    r = sub.add_parser("run-scheduler", help="loop: run-due every --interval seconds")
    r.add_argument("--interval", type=int, default=300)
    r.add_argument("--once", action="store_true")
    r.set_defaults(fn=cmd_run_scheduler)
    sub.add_parser("status", help="data freshness, job health, credits").set_defaults(fn=cmd_status)
    c = sub.add_parser("coverage", help="observed bookmaker × market coverage for an ET date")
    c.add_argument("--date")
    c.set_defaults(fn=cmd_coverage)
    v = sub.add_parser("verify", help="sanity-check stored quotes for a date (fields, pairs, overround, cross-book agreement)")
    v.add_argument("--date")
    v.add_argument("--book", action="append")
    v.set_defaults(fn=cmd_verify)
    cl = sub.add_parser("closing", help="last pre-tip observation of every quote for an event")
    cl.add_argument("--event", required=True)
    cl.set_defaults(fn=cmd_closing)
    b = sub.add_parser("budget", help="estimate monthly credit use for the current market/book config")
    b.add_argument("--games-per-day", type=float, default=7.5)
    b.add_argument("--snapshots-per-game", type=float, default=4)
    b.add_argument("--days", type=int, default=30)
    b.set_defaults(fn=cmd_budget)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    settings = Settings.from_env()
    register_secret(settings.odds_api_key.reveal())
    register_secret(settings.propline_api_key.reveal())
    return args.fn(settings, args)


if __name__ == "__main__":
    sys.exit(main())
