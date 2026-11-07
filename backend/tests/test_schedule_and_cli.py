import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine, func, select

from nba_edge.config import Secret, Settings
from nba_edge.db.schema import odds_snapshots, slot_completions, snapshot_runs
from nba_edge.pipeline.schedule import due_slots, missed_slots, plan_slots, run_due
from conftest import NOW

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_plan_slots_fixed_pretip_and_closing():
    s = Settings()
    games = [{"provider_event_id": "evt1", "game_date_et": "2026-11-07", "commence_time": NOW + timedelta(hours=4)},
             {"provider_event_id": "other-day", "game_date_et": "2026-11-08", "commence_time": NOW + timedelta(days=1)}]
    slots = plan_slots(games, date(2026, 11, 7), s)
    keys = [x.key for x in slots]
    assert keys == ["2026-11-07:fixed:10:00", "2026-11-07:fixed:15:00", "2026-11-07:pretip:60:evt1", "2026-11-07:closing:10:evt1"]
    fixed10 = slots[0]
    assert fixed10.due_at == datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)  # 10:00 EST = 15:00 UTC
    closing = slots[-1]
    assert closing.due_at == NOW + timedelta(hours=3, minutes=50) and closing.deadline == NOW + timedelta(hours=4)
    assert [x.key for x in due_slots(slots, NOW + timedelta(minutes=5), set())] == ["2026-11-07:fixed:15:00"]
    assert [x.key for x in missed_slots(slots, NOW + timedelta(minutes=5), set())] == ["2026-11-07:fixed:10:00"]


def test_run_due_serves_each_slot_once_and_records_missed(engine, settings, provider):
    kw = dict(transport=provider.transport(), sleep=lambda s: None)
    s1 = run_due(engine, settings, now=NOW + timedelta(minutes=5), **kw)          # 15:05 ET
    assert s1["status"] == "ok" and s1["events_refresh"]["status"] == "ok"
    assert [r["reason"] for r in s1["runs"]] == ["fixed"] and s1["runs"][0]["event_ids"] == ["evt1", "evt2"]
    assert s1["missed"] == ["2026-11-07:fixed:10:00"]
    calls = len(provider.requests)
    s2 = run_due(engine, settings, now=NOW + timedelta(minutes=10), **kw)
    assert s2["runs"] == [] and len(provider.requests) == calls, "a served slot is never paid for twice"
    s3 = run_due(engine, settings, now=NOW + timedelta(hours=3, minutes=5), **kw)  # evt1 T-55
    assert [(r["reason"], r["event_ids"]) for r in s3["runs"]] == [("pretip", ["evt1"])]
    s4 = run_due(engine, settings, now=NOW + timedelta(hours=3, minutes=52), **kw)  # evt1 T-8
    assert [(r["reason"], r["event_ids"]) for r in s4["runs"]] == [("closing", ["evt1"])]
    s5 = run_due(engine, settings, now=NOW + timedelta(hours=7, minutes=30), **kw)  # after evt2 tip, its slots never ran
    assert set(s5["missed"]) == {"2026-11-07:pretip:60:evt2", "2026-11-07:closing:10:evt2"}
    with engine.connect() as c:
        n_missed = c.execute(select(func.count()).select_from(slot_completions).where(slot_completions.c.status == "missed")).scalar()
    assert n_missed == 3


def test_run_due_without_key_makes_no_calls(engine, settings, provider):
    nokey = Settings(database_url=settings.database_url, odds_api_key=Secret(None))
    s = run_due(engine, nokey, now=NOW, transport=provider.transport())
    assert s["status"] == "blocked_no_key" and provider.requests == []


def _cli(args, env):
    return subprocess.run([sys.executable, "-m", "nba_edge", *args], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60)


def test_cli_end_to_end_over_real_http(tmp_path, provider):
    # Make the synthetic games "today" relative to the real clock, since the CLI uses real time.
    now = datetime.now(timezone.utc)
    for i, ev_id in enumerate(list(provider.events)):
        t = (now + timedelta(hours=2 + i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        provider.events[ev_id]["commence_time"] = t
        provider.odds[ev_id]["commence_time"] = t
    server, base = provider.serve()
    try:
        db = tmp_path / "cli.sqlite3"
        key = "cli-dummy-key-not-real-1234"
        env = {**os.environ, "NBA_EDGE_DATABASE_URL": f"sqlite:///{db}", "ODDS_API_KEY": key, "ODDS_API_BASE_URL": base,
               "ODDS_API_MARKETS": "player_points,player_rebounds", "ODDS_MONTHLY_CREDIT_BUDGET": "1000", "PYTHONPATH": BACKEND}
        r = _cli(["snapshot"], env)
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        # (an event whose tip-off falls on the next ET day is not "today" — count only what was planned)
        assert out["status"] == "ok" and out["events_fetched"] == out["events_planned"] >= 1 and out["rows_inserted"] > 0
        for cmd in (["status"], ["coverage"], ["budget"], ["config"]):
            rr = _cli(cmd, env)
            assert rr.returncode == 0, rr.stderr
            assert key not in rr.stdout + rr.stderr
        assert key not in r.stdout + r.stderr
        eng = create_engine(f"sqlite:///{db}")
        with eng.connect() as c:
            assert c.execute(select(func.count()).select_from(odds_snapshots)).scalar() == out["rows_inserted"]
            assert c.execute(select(snapshot_runs.c.status)).scalar() == "ok"
        eng.dispose()
        blocked = _cli(["snapshot"], {k: v for k, v in env.items() if k != "ODDS_API_KEY"})
        assert blocked.returncode == 2 and "BLOCKED" in blocked.stderr
        assert json.loads(blocked.stdout)["status"] == "blocked_no_key"
    finally:
        server.shutdown()
