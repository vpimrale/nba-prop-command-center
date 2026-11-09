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


def _lock_or_explain(engine, settings, holder, force=False) -> bool:
    from .pipeline.ops import acquire_lock
    ok, row = acquire_lock(engine, holder, datetime.now(timezone.utc), settings.lock_stale_min)
    if not ok and not force:
        print(f"LOCKED: another scheduler ({row['holder']}) heartbeat {row['heartbeat_at'].isoformat()} holds this database. "
              f"Not starting a second one (it would double-spend provider requests). Use --force only if that process is gone.", file=sys.stderr)
        return False
    if not ok and force:
        from sqlalchemy import update
        from .db.paper_schema import scheduler_lock
        with engine.begin() as conn:
            conn.execute(update(scheduler_lock).values(holder=holder, heartbeat_at=datetime.now(timezone.utc)))
    return True


def cmd_run_due(settings, args) -> int:
    """One tick (for cron / systemd timers / Task Scheduler). Skips if a scheduler loop holds the lock."""
    from .pipeline.ops import holder_id, ping, release_lock
    engine = _engine(settings)
    holder = holder_id()
    if not _lock_or_explain(engine, settings, holder):
        return 3
    try:
        summary = run_due(engine, settings)
    finally:
        release_lock(engine, holder)
    _print(summary)
    failed = summary.get("status") != "ok" or any(isinstance(v, dict) and v.get("status") == "failed" for v in (summary.get("research") or {}).values())
    ping(settings, failed=failed)
    return 0 if summary.get("status") == "ok" else 2


def cmd_run_scheduler(settings, args) -> int:
    """Long-running loop. Restart-safe: all due-ness lives in the database, so a restarted
    process catches up on its first tick. SIGTERM/SIGINT stop it cleanly between ticks."""
    import signal
    import threading
    from .pipeline.ops import heartbeat, holder_id, ping, release_lock
    from .pipeline.ops import acquire_lock
    engine = _engine(settings)
    holder = holder_id()
    interval = args.interval or settings.tick_expected_min * 60
    stop = threading.Event()

    def _stop(signum, _frame):
        log.info("signal %s: stopping after the current tick", signum)
        stop.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except ValueError:  # not in main thread (tests)
            pass
    if not _lock_or_explain(engine, settings, holder, force=args.force):
        if args.once:
            return 3
        # Service mode: wait as a standby instead of exiting. Exiting would make systemd /
        # launchd / Task Scheduler restart us in a loop until they give up; waiting takes the
        # lock as soon as the holder releases it, dies, or its heartbeat goes stale.
        log.warning("waiting for the scheduler lock (checking every %ss)", interval)
        while True:
            if stop.wait(interval):
                return 3
            if acquire_lock(engine, holder, datetime.now(timezone.utc), settings.lock_stale_min)[0]:
                log.info("scheduler lock acquired")
                break
    log.info("scheduler %s: tick every %ss", holder, interval)
    try:
        while not stop.is_set():
            failed = True
            try:
                if not heartbeat(engine, holder, datetime.now(timezone.utc)) and not _lock_or_explain(engine, settings, holder):
                    return 3
                s = run_due(engine, settings)
                bad = [k for k, v in (s.get("research") or {}).items() if isinstance(v, dict) and v.get("status") == "failed"]
                failed = s.get("status") == "failed" or bool(bad)
                log.info("tick: status=%s runs=%d missed=%d%s%s", s.get("status"), len(s["runs"]), len(s["missed"]),
                         f" research_failed={bad}" if bad else "", f" gap={s['gap']['gap_min']}min" if s.get("gap") else "")
            except Exception:  # keep the loop alive; the failure is logged and visible in job_runs
                log.exception("scheduler tick failed")
            heartbeat(engine, holder, datetime.now(timezone.utc))
            ping(settings, failed=failed)
            if args.once:
                break
            stop.wait(interval)
    finally:
        release_lock(engine, holder)
    return 0


def cmd_health(settings, args) -> int:
    from .pipeline.ops import LEVELS, health_report
    r = health_report(_engine(settings), settings)
    _print(r)
    return LEVELS[r["level"]]


def cmd_maintenance(settings, args) -> int:
    from .pipeline.ops import run_maintenance
    r = run_maintenance(_engine(settings), settings, datetime.now(timezone.utc))
    _print(r)
    return 0 if r["status"] == "ok" else 2


def cmd_paper(settings, args) -> int:
    from .model.paper import calibration_track, paper_report, verify_hashes
    engine = _engine(settings)
    rep = paper_report(engine)
    if not args.full:
        rep.pop("recent", None)
        for grp in (rep.get("by") or {}).values():
            for v in grp.values():
                v.pop("reliability", None)
        if rep.get("calibration"):
            rep["calibration"] = {k: v for k, v in rep["calibration"].items() if k != "reliability"}
    out = {"paper": rep, "ledger_integrity": verify_hashes(engine)}
    if args.all_quotes:
        out["all_quotes_calibration"] = calibration_track(engine)
    _print(out)
    return 0


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


# ----------------------------------------------------------------- research / model
def _espn(engine):
    from .pipeline.snapshots import RequestRecorder
    from .research.espn import EspnClient
    return EspnClient(on_request=RequestRecorder(engine, "espn"))


def cmd_ingest_espn(settings, args) -> int:
    from datetime import date, timedelta
    from .research.ingest import ingest_dates
    engine = _engine(settings)
    today = datetime.now(timezone.utc).astimezone(ET).date()
    start = date.fromisoformat(args.start) if args.start else today - timedelta(days=args.days)
    end = date.fromisoformat(args.end) if args.end else today
    c = _espn(engine)
    try:
        r = ingest_dates(engine, c, start, end, refetch=args.refetch)
    finally:
        c.close()
    _print(r)
    return 0 if r["status"] in ("ok", "partial") else 2


def cmd_ingest_injuries(settings, args) -> int:
    from .research.ingest import ingest_injuries_espn, ingest_injuries_propline
    engine = _engine(settings)
    out = {}
    c = _espn(engine)
    try:
        out["espn"] = ingest_injuries_espn(engine, c)
    finally:
        c.close()
    if settings.propline_api_key and not args.espn_only:
        from .providers.propline import PropLineClient
        from .pipeline.snapshots import RequestRecorder
        pc = PropLineClient(settings, on_request=RequestRecorder(engine, "propline"))
        try:
            out["propline"] = ingest_injuries_propline(engine, pc)
        except Exception as e:  # secondary source: report, don't fail the command
            out["propline"] = {"status": "failed", "detail": str(e)}
        finally:
            pc.close()
    _print(out)
    return 0


def cmd_project(settings, args) -> int:
    from dataclasses import replace
    from .model.config import ModelConfig
    from .model.run import run_slate
    cfg = replace(ModelConfig(), n_sims=args.sims)
    r = run_slate(_engine(settings), date_et=args.date, cfg=cfg, provider=settings.odds_provider, seed=args.seed)
    _print(r)
    return 0 if r["status"] in ("ok", "no_games") else 2


def cmd_grade(settings, args) -> int:
    from .model.grading import grade_pending
    from .model.paper import grade_paper
    engine = _engine(settings)
    _print({"paper_ledger": grade_paper(engine), "all_quotes": grade_pending(engine),
            "note": "Graded against final ESPN box scores. Paper only — see `nba-edge paper` for the report."})
    return 0


def cmd_backtest(settings, args) -> int:
    from datetime import date
    from dataclasses import replace
    from .model.backtest import run_backtest
    from .model.config import ModelConfig
    r = run_backtest(_engine(settings), date.fromisoformat(args.start), date.fromisoformat(args.end), every_n_days=args.every,
                     n_sims=args.sims, cfg=replace(ModelConfig()), max_dates=args.max_dates)
    s = r["summary"]
    _print({"backtest_id": r["backtest_id"], "dates": r["dates"], "rows": r["rows"], "line_method": s.get("line_method"),
            "all_markets": {k: {kk: vv for kk, vv in v.items() if kk != "reliability"} for k, v in (s.get("all_markets") or {}).items() if v},
            "markets": {k: {"n": v["n"], "mae": v["mae_mean"], "bias": v["bias_mean_minus_actual"], "brier": v["model"]["brier"],
                            "brier_hit_rate": (v["raw_L10_hit_rate_baseline"] or {}).get("brier"), "ece": v["model"]["ece"]} for k, v in s["markets"].items()}})
    return 0


def cmd_prune(settings, args) -> int:
    from .model.retention import prune_evaluations
    _print(prune_evaluations(_engine(settings), keep_days=args.keep_days if args.keep_days is not None else settings.prune_keep_days))
    return 0


def cmd_serve(settings, args) -> int:
    from .dashboard.server import serve
    httpd = serve(_engine(settings), args.host, args.port, settings=settings)
    print(f"dashboard: http://{args.host}:{httpd.server_address[1]}/  (Ctrl-C to stop; local, read-only)", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nba-edge", description="NBA Quant Edge backend: odds snapshots (default source: PropLine free tier)")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--log-file", default=None, help="also append logs to this file (rotated at 10 MB, 5 kept)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("config", help="print effective configuration (no secrets)").set_defaults(fn=cmd_config)
    sub.add_parser("init-db", help="create/upgrade the database schema").set_defaults(fn=cmd_init_db)
    sub.add_parser("refresh-events", help="fetch upcoming NBA events from the odds provider").set_defaults(fn=cmd_refresh_events)
    s = sub.add_parser("snapshot", help="capture odds now for today's (ET) upcoming games, or specific events")
    s.add_argument("--event", action="append", help="provider event id (repeatable)")
    s.add_argument("--date", help="ET game date YYYY-MM-DD (default: today ET)")
    s.add_argument("--reason", default="manual")
    s.set_defaults(fn=cmd_snapshot)
    sub.add_parser("run-due", help="one scheduler tick (cron/timer mode): odds slots + research jobs that are due").set_defaults(fn=cmd_run_due)
    r = sub.add_parser("run-scheduler", help="long-running loop: one tick every --interval seconds (single instance per database)")
    r.add_argument("--interval", type=int, default=0, help="seconds (default: SCHEDULER_TICK_MIN x 60)")
    r.add_argument("--once", action="store_true", help="run one tick and exit (exits 3 if another scheduler holds the lock)")
    r.add_argument("--force", action="store_true", help="take the lock even if another holder's heartbeat is fresh")
    r.set_defaults(fn=cmd_run_scheduler)
    sub.add_parser("health", help="freshness/failure/gap checks; exit 0 ok, 1 warn, 2 critical").set_defaults(fn=cmd_health)
    sub.add_parser("maintenance", help="daily job now: prune, ledger hash check, SQLite backup").set_defaults(fn=cmd_maintenance)
    pp = sub.add_parser("paper", help="paper-trading report (REAL lines, graded; separate from simulated backtests)")
    pp.add_argument("--full", action="store_true", help="include reliability bins and recent recommendations")
    pp.add_argument("--all-quotes", action="store_true", help="also show the all-quotes calibration track")
    pp.set_defaults(fn=cmd_paper)
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
    ie = sub.add_parser("ingest-espn", help="ESPN schedules + final box scores (free) for a date range")
    ie.add_argument("--start"); ie.add_argument("--end"); ie.add_argument("--days", type=int, default=2)
    ie.add_argument("--refetch", action="store_true")
    ie.set_defaults(fn=cmd_ingest_espn)
    ii = sub.add_parser("ingest-injuries", help="injury reports: ESPN (+ PropLine's free endpoint if a key is set)")
    ii.add_argument("--espn-only", action="store_true")
    ii.set_defaults(fn=cmd_ingest_injuries)
    pj = sub.add_parser("project", help="project every player on today's (ET) slate and evaluate live FanDuel/Fanatics quotes")
    pj.add_argument("--date"); pj.add_argument("--sims", type=int, default=10_000); pj.add_argument("--seed", type=int)
    pj.set_defaults(fn=cmd_project)
    gr = sub.add_parser("grade", help="grade stored evaluations against final box scores (real lines, real outcomes)")
    gr.set_defaults(fn=cmd_grade)
    bt = sub.add_parser("backtest", help="walk-forward projection backtest on historical box scores (SIMULATED lines)")
    bt.add_argument("--start", required=True); bt.add_argument("--end", required=True)
    bt.add_argument("--every", type=int, default=3); bt.add_argument("--sims", type=int, default=2000); bt.add_argument("--max-dates", type=int)
    bt.set_defaults(fn=cmd_backtest)
    pr = sub.add_parser("prune", help="retention: drop old non-final model evaluations (keeps each game's last pre-tip run)")
    pr.add_argument("--keep-days", type=int, default=None, help="default: PRUNE_KEEP_DAYS (3)")
    pr.set_defaults(fn=cmd_prune)
    sv = sub.add_parser("serve", help="local read-only research dashboard")
    sv.add_argument("--host", default="127.0.0.1"); sv.add_argument("--port", type=int, default=8765)
    sv.set_defaults(fn=cmd_serve)
    b = sub.add_parser("budget", help="estimate monthly credit use for the current market/book config")
    b.add_argument("--games-per-day", type=float, default=7.5)
    b.add_argument("--snapshots-per-game", type=float, default=4)
    b.add_argument("--days", type=int, default=30)
    b.set_defaults(fn=cmd_budget)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level, log_file=args.log_file)
    settings = Settings.from_env()
    register_secret(settings.odds_api_key.reveal())
    register_secret(settings.propline_api_key.reveal())
    register_secret(settings.healthcheck_ping_url.reveal())
    return args.fn(settings, args)


if __name__ == "__main__":
    sys.exit(main())
