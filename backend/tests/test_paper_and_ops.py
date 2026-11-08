"""Paper-trading ledger (immutable, graded at the recommendation price, CLV, report) and
operations (lock, gaps, catch-up, maintenance/backup, health, CLI) — synthetic data only."""
import gzip
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import DBAPIError

from nba_edge.config import Settings
from nba_edge.db.paper_schema import paper_outcomes, paper_recommendations, scheduler_lock
from nba_edge.db.research_schema import nba_games, nba_player_games, prop_grades
from nba_edge.db.schema import job_runs, odds_current, odds_snapshots, snapshot_runs
from nba_edge.model.config import MODEL_VERSION, DecisionConfig, ModelConfig
from nba_edge.odds.normalize import american_to_decimal
from synthetic_league import SEASON, add_odds, build

START = datetime(2026, 9, 25, tzinfo=timezone.utc)
FAST = replace(ModelConfig(), n_sims=3000)
# Gates relaxed ONLY in tests, so the synthetic slate produces candidates to record.
LOOSE = replace(DecisionConfig(), min_ev_main=-1.0, min_ev_alt=-1.0, max_model_market_gap=1.0, max_main_line_gap_for_alt=1.0,
                calibration_margin_main=0.0, calibration_margin_alt=0.0, min_games_sample=0, min_games_this_season=0)
QUOTES = [("fanduel", "player_points", "PTS", "Synth AAA Player0", "over", 17.5, -110, False),
          ("fanduel", "player_points", "PTS", "Synth AAA Player0", "under", 17.5, -110, False),
          ("fanatics", "player_points", "PTS", "Synth AAA Player0", "over", 17.5, -105, False),
          ("fanatics", "player_points", "PTS", "Synth AAA Player0", "under", 17.5, -115, False),
          ("fanduel", "player_rebounds", "REB", "Synth BBB Player4", "over", 4.5, -120, False),
          ("fanduel", "player_rebounds", "REB", "Synth BBB Player4", "under", 4.5, -105, False)]
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def league(engine):
    return build(engine, START)


def _snapshot_current(engine, at):
    """Mirror odds_current into the append-only snapshot table (what a capture run does)."""
    with engine.begin() as conn:
        rid = conn.execute(insert(snapshot_runs).values(reason="fixed", provider="propline", started_at=at, finished_at=at, status="ok")).inserted_primary_key[0]
        cols = [c.name for c in odds_current.columns if c.name in odds_snapshots.c and c.name != "id"]
        rows = [dict({k: r[k] for k in cols}, run_id=rid, captured_at=at, snapshot_reason="fixed", change_type="new")
                for r in conn.execute(select(odds_current)).mappings()]
        conn.execute(insert(odds_snapshots), rows)
    return rid


def _price_change(engine, quote_key, at, american, status="active", reason="closing"):
    with engine.begin() as conn:
        rid = conn.execute(insert(snapshot_runs).values(reason=reason, provider="propline", started_at=at, finished_at=at, status="ok")).inserted_primary_key[0]
        base = dict(conn.execute(select(odds_snapshots).where(odds_snapshots.c.quote_key == quote_key).order_by(odds_snapshots.c.id.desc()).limit(1)).mappings().one())
        base.pop("id")
        conn.execute(insert(odds_snapshots).values(**{**base, "run_id": rid, "captured_at": at, "snapshot_reason": reason,
                                                      "change_type": "removed" if status == "removed" else "price_change",
                                                      "american_odds": american if american else base["american_odds"],
                                                      "decimal_odds": american_to_decimal(american) if american else base["decimal_odds"], "market_status": status}))


def _recs(engine):
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(paper_recommendations).order_by(paper_recommendations.c.id))]


def _setup_recs(engine, league):
    from nba_edge.model.run import run_slate
    add_odds(engine, league["now"], league["slate"], QUOTES)
    _snapshot_current(engine, league["now"] - timedelta(minutes=1))
    r = run_slate(engine, now=league["now"], cfg=FAST, dcfg=LOOSE, seed=7)
    assert r["n_candidates"] > 0, "loosened gates must produce candidates on the synthetic slate"
    return r


def test_paper_ledger_records_first_qualification_only_and_is_immutable(engine, league):
    from nba_edge.model.paper import content_hash, verify_hashes
    from nba_edge.model.run import run_slate
    r = _setup_recs(engine, league)
    recs = _recs(engine)
    assert len(recs) == r["n_candidates"] and "paper ledger" in (r["detail"] or "")
    with engine.connect() as conn:
        cur = {q.quote_key: q for q in conn.execute(select(odds_current))}
    for x in recs:
        q = cur[x["quote_key"]]
        assert (x["american_odds"], x["line"], x["bookmaker"], x["side"]) == (q.american_odds, q.line, q.bookmaker, q.side), "exact captured price"
        assert x["model_version"] == MODEL_VERSION and x["recommended_at"] == league["now"] and x["run_id"] == r["id"]
        assert x["projection_mean"] is not None and x["odds_snapshot_id"] is not None and x["minutes_mean"] > 0
        assert 0 < x["model_prob"] < 1 and x["ev"] is not None and x["content_sha256"] == content_hash(x)
        assert x["commence_time"] == league["slate"]["start_time"] and x["team"] in ("AAA", "BBB")
    # a later run at a different price must not add or alter the original recommendation
    first = {x["quote_key"]: x["american_odds"] for x in recs}
    with engine.begin() as conn:
        conn.execute(update(odds_current).values(american_odds=+120, decimal_odds=2.2))
    run_slate(engine, now=league["now"] + timedelta(minutes=30), cfg=FAST, dcfg=LOOSE, seed=8)
    again = _recs(engine)
    assert len(again) == len(recs) and {x["quote_key"]: x["american_odds"] for x in again} == first
    # the database itself refuses edits and deletes
    with pytest.raises(DBAPIError):
        with engine.begin() as conn:
            conn.execute(update(paper_recommendations).values(american_odds=999))
    with pytest.raises(DBAPIError):
        with engine.begin() as conn:
            conn.execute(delete(paper_recommendations))
    assert verify_hashes(engine) == {"checked": len(recs), "mismatched_ids": []}


def _finish_game(engine, league, aaa_pts=20, bbb4_played=True):
    s = league["slate"]
    base = dict(espn_event_id=s["espn_event_id"], position="G", is_home=True, starter=True, dnp_reason=None, season=SEASON, season_type=2,
                date_et=s["date_et"], start_time=s["start_time"], ast=3, fg3m=1, stl=1, blk=0, tov=2)
    with engine.begin() as conn:
        conn.execute(update(nba_games).where(nba_games.c.espn_event_id == s["espn_event_id"]).values(completed=True, box_ingested=True))
        conn.execute(insert(nba_player_games), [
            {**base, "athlete_id": "AAA0", "player_name": "Synth AAA Player0", "team": "AAA", "opponent": "BBB", "played": True, "minutes": 33, "pts": aaa_pts, "reb": 5},
            {**base, "athlete_id": "BBB4", "player_name": "Synth BBB Player4", "team": "BBB", "opponent": "AAA", "is_home": False,
             "played": bbb4_played, "minutes": 30 if bbb4_played else 0, "pts": 10 if bbb4_played else 0, "reb": 4 if bbb4_played else 0}])


def test_paper_grading_uses_recommendation_price_and_closing_snapshot(engine, league):
    from nba_edge.model.paper import grade_paper, paper_report
    _setup_recs(engine, league)
    recs = _recs(engine)
    tip = league["slate"]["start_time"]
    pts_over = next((x for x in recs if x["market_code"] == "PTS" and x["side"] == "over"), None)
    others = [x for x in recs if x is not pts_over]
    if pts_over:
        _price_change(engine, pts_over["quote_key"], tip - timedelta(minutes=8), -140)          # market moved toward the over
    if others:
        _price_change(engine, others[0]["quote_key"], tip - timedelta(minutes=30), None, status="removed")  # pulled before tip
    _price_change(engine, recs[0]["quote_key"], tip + timedelta(minutes=5), -500, reason="manual")     # after tip: never the close
    _finish_game(engine, league, aaa_pts=20, bbb4_played=False)
    g = grade_paper(engine, now=tip + timedelta(hours=3))
    assert g["graded"] == len(recs)
    assert grade_paper(engine, now=tip + timedelta(hours=4))["graded"] == 0, "graded exactly once"
    with engine.connect() as conn:
        out = {o.recommendation_id: o for o in conn.execute(select(paper_outcomes))}
    for x in recs:
        o = out[x["id"]]
        if x["athlete_id"] == "BBB4":
            assert o.result == "void" and o.profit_units == 0 and o.actual is None
            continue
        exp = "win" if (20 > x["line"]) == (x["side"] == "over") else "loss"
        assert o.result == exp and o.actual == 20
        assert o.profit_units == pytest.approx(x["decimal_odds"] - 1 if exp == "win" else -1.0), "profit at the RECOMMENDATION price"
        assert o.closing_american != -500, "a post-tip price is never used as the close"
    if pts_over:
        o = out[pts_over["id"]]
        assert o.closing_american == -140 and o.closing_offered is True
        assert o.clv_prob == pytest.approx(140 / 240 - 1 / pts_over["decimal_odds"], abs=1e-6) and o.clv_prob > 0
        assert o.closing_minutes_before_tip == pytest.approx(8.0)
    if others:
        o = out[others[0]["id"]]
        assert o.closing_offered is False and o.closing_american == others[0]["american_odds"], "pulled quote: last active price, flagged"
    with pytest.raises(DBAPIError):
        with engine.begin() as conn:
            conn.execute(update(paper_outcomes).values(result="win"))
    rep = paper_report(engine, now=tip + timedelta(hours=5))
    assert rep["real_money_betting"] == "disabled" and rep["profitability_claim"] == "none"
    assert rep["evidence_status"]["status"] == "insufficient_sample" and rep["n_graded"] == len(recs) and rep["n_pending"] == 0
    o = rep["overall"]
    assert o["n"] == len(recs) and o["voids"] == sum(x["athlete_id"] == "BBB4" for x in recs)
    assert set(rep["by"]) == {"market", "sportsbook", "probability_bucket", "side", "line_type", "model_version"}
    assert sum(v["n"] for v in rep["by"]["sportsbook"].values()) == len(recs)
    if o["wins"] + o["losses"]:
        assert o["brier_model"] is not None and "roi" in o and rep["calibration"]["n"] == o["wins"] + o["losses"]


def test_paper_and_simulated_tracks_are_separate(engine, league):
    from nba_edge.dashboard import api
    from nba_edge.model.backtest import run_backtest
    _setup_recs(engine, league)
    d = (START + timedelta(days=30)).date()
    run_backtest(engine, d, d + timedelta(days=2), every_n_days=2, n_sims=500, cfg=FAST)
    paper = api.paper(engine)
    sim = api.simulated(engine)
    assert "backtest" not in json.dumps(paper).lower(), "paper view never carries simulated results"
    assert "SIMULATED" in sim["label"] and "paper" not in sim and sim["backtest"]["summary"]["line_method"].startswith("SIMULATED")
    assert paper["paper"]["track"].startswith("PAPER") and paper["all_quotes"]["track"].startswith("ALL QUOTES")
    assert "roi" not in json.dumps(paper["all_quotes"]), "the all-quotes calibration track reports no ROI"


# ------------------------------------------------------------------ operations
NOW = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)


def test_scheduler_lock_single_instance_and_stale_takeover(engine):
    from nba_edge.pipeline.ops import acquire_lock, heartbeat, release_lock
    assert acquire_lock(engine, "host:1", NOW, 15)[0]
    ok, row = acquire_lock(engine, "host:2", NOW + timedelta(minutes=5), 15)
    assert not ok and row["holder"] == "host:1", "a second scheduler is refused while the first is alive"
    assert heartbeat(engine, "host:1", NOW + timedelta(minutes=10))
    assert not acquire_lock(engine, "host:2", NOW + timedelta(minutes=20), 15)[0], "heartbeat keeps the lock"
    assert acquire_lock(engine, "host:2", NOW + timedelta(minutes=26), 15)[0], "a dead holder's lock is taken over"
    assert not heartbeat(engine, "host:1", NOW + timedelta(minutes=27)), "the old holder learns it lost the lock"
    with engine.connect() as conn:
        assert "took over stale lock" in conn.execute(select(job_runs.c.detail).where(job_runs.c.job_name == "scheduler_lock")).scalar()
    release_lock(engine, "host:2")
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(scheduler_lock)).scalar() == 0


def test_gap_detection_and_catch_up_window(engine):
    from nba_edge.pipeline.ops import record_gap_if_any
    from nba_edge.pipeline.schedule import catch_up_start
    with engine.begin() as conn:
        conn.execute(insert(job_runs).values(job_name="scheduler_tick", started_at=NOW, finished_at=NOW, status="ok"))
    assert record_gap_if_any(engine, NOW + timedelta(minutes=6), 5) is None
    gap = record_gap_if_any(engine, NOW + timedelta(hours=3), 5)
    assert gap["gap_min"] == 180.0
    with engine.connect() as conn:
        assert conn.execute(select(job_runs.c.status).where(job_runs.c.job_name == "scheduler_gap")).scalar() == "missed"
    today = date(2026, 11, 7)
    assert catch_up_start(engine, today) == today - timedelta(days=1), "no prior ingest: yesterday"
    with engine.begin() as conn:
        conn.execute(insert(job_runs).values(job_name="ingest_espn", started_at=datetime(2026, 11, 3, 18, tzinfo=timezone.utc), status="ok"))
    assert catch_up_start(engine, today) == date(2026, 11, 2), "after downtime: the day before the last good ingest"
    with engine.begin() as conn:
        conn.execute(update(job_runs).where(job_runs.c.job_name == "ingest_espn").values(started_at=datetime(2026, 10, 1, tzinfo=timezone.utc)))
    assert catch_up_start(engine, today) == today - timedelta(days=7), "capped"


def test_run_due_survives_an_odds_failure(engine, settings, provider, monkeypatch):
    from nba_edge.pipeline import schedule
    monkeypatch.setattr(schedule, "plan_slots", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    s = schedule.run_due(engine, settings, now=NOW, transport=provider.transport(), sleep=lambda x: None)
    assert s["status"] == "failed" and "boom" in s["error"]
    with engine.connect() as conn:
        t = conn.execute(select(job_runs).where(job_runs.c.job_name == "scheduler_tick")).mappings().one()
    assert t["status"] == "failed" and "boom" in t["detail"] and t["finished_at"] is not None


def test_maintenance_backup_rotation_and_due(engine, tmp_path):
    from nba_edge.pipeline.ops import maintenance_due, run_maintenance
    s = replace(Settings(), backup_dir=str(tmp_path / "bk"), backup_keep=2)
    morning = datetime(2026, 11, 7, 11, tzinfo=timezone.utc)   # 06:00 ET
    assert not maintenance_due(engine, s, datetime(2026, 11, 7, 8, tzinfo=timezone.utc)), "not before 05:00 ET"
    assert maintenance_due(engine, s, morning)
    for i in range(3):
        r = run_maintenance(engine, s, morning + timedelta(minutes=i))
        assert r["status"] == "ok" and r["ledger_hash_check"]["mismatched_ids"] == []
    assert not maintenance_due(engine, s, morning + timedelta(hours=5)), "once per ET day"
    assert maintenance_due(engine, s, morning + timedelta(days=1))
    if engine.dialect.name != "sqlite":
        assert r["backup"]["status"] == "skipped"
        return
    files = sorted(os.listdir(tmp_path / "bk"))
    assert len(files) == 2 and all(f.endswith(".sqlite3.gz") for f in files), "rotation keeps the newest N"
    raw = tmp_path / "restored.sqlite3"
    raw.write_bytes(gzip.open(tmp_path / "bk" / files[-1]).read())
    tables = {r[0] for r in sqlite3.connect(raw).execute("select name from sqlite_master where type='table'")}
    assert {"paper_recommendations", "odds_snapshots", "job_runs"} <= tables, "backup is a restorable database"


def test_health_levels(engine):
    from nba_edge.pipeline.ops import health_report
    s = Settings()
    r = health_report(engine, s, NOW)
    assert r["level"] == "critical" and any(c["check"] == "scheduler" and c["level"] == "critical" for c in r["checks"])
    with engine.begin() as conn:
        for job in ("scheduler_tick", "ingest_espn", "ingest_injuries:espn", "maintenance"):
            conn.execute(insert(job_runs).values(job_name=job, started_at=NOW - timedelta(minutes=2), finished_at=NOW, status="ok"))
    r = health_report(engine, s, NOW)
    assert r["level"] == "ok", [c for c in r["checks"] if c["level"] != "ok"]
    with engine.begin() as conn:
        conn.execute(insert(job_runs).values(job_name="model_run", started_at=NOW - timedelta(minutes=1), status="failed", detail="x"))
    r = health_report(engine, s, NOW)
    assert r["level"] == "warn" and next(c for c in r["checks"] if c["check"] == "job_failures_24h")["counts"] == {"model_run": 1}
    r = health_report(engine, s, NOW + timedelta(hours=2))
    assert r["level"] == "critical", "a scheduler silent for 2 h is critical"


def _cli(args, env):
    return subprocess.run([sys.executable, "-m", "nba_edge", *args], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120)


def test_cli_health_scheduler_once_lock_and_paper(tmp_path):
    url = f"sqlite:///{tmp_path / 'cli.sqlite3'}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PROPLINE", "ODDS_", "HEALTHCHECK"))}
    env.update(NBA_EDGE_DATABASE_URL=url, RESEARCH_JOBS="0", NBA_EDGE_BACKUP_DIR=str(tmp_path / "bk"))
    h = _cli(["health"], env)
    assert h.returncode == 2 and json.loads(h.stdout)["level"] == "critical"
    r = _cli(["run-scheduler", "--once", "--interval", "1"], env)
    assert r.returncode == 0, r.stderr
    from nba_edge.db.engine import make_engine
    eng = make_engine(url)
    with eng.begin() as conn:
        assert conn.execute(select(func.count()).select_from(scheduler_lock)).scalar() == 0, "lock released on exit"
        conn.execute(insert(scheduler_lock).values(name="scheduler", holder="otherhost:1", heartbeat_at=datetime.now(timezone.utc)))
    r = _cli(["run-due"], env)
    assert r.returncode == 3 and "LOCKED" in r.stderr, "never two schedulers on one database"
    p = _cli(["paper"], env)
    out = json.loads(p.stdout)
    assert p.returncode == 0 and out["paper"]["n_recommendations"] == 0 and out["paper"]["real_money_betting"] == "disabled"
    m = _cli(["maintenance"], env)
    assert m.returncode == 0 and json.loads(m.stdout)["backup"]["status"] == "ok"
    eng.dispose()


def test_clv_is_unavailable_not_zero_without_a_later_capture(engine, league):
    """Overnight gap: no odds capture after the recommendation -> CLV must be None, not 0."""
    from nba_edge.model.grading import grade_pending
    from nba_edge.model.paper import grade_paper, paper_report
    _setup_recs(engine, league)
    _finish_game(engine, league)
    assert grade_paper(engine, now=league["slate"]["start_time"] + timedelta(hours=3))["graded"] > 0
    grade_pending(engine)
    with engine.connect() as conn:
        assert all(o.clv_prob is None for o in conn.execute(select(paper_outcomes.c.clv_prob)))
        assert all(g.clv_prob is None for g in conn.execute(select(prop_grades.c.clv_prob)))
        assert all(o.closing_american is not None for o in conn.execute(select(paper_outcomes.c.closing_american)))
    assert paper_report(engine)["overall"]["clv"]["n"] == 0


def test_all_quotes_grading_uses_only_each_quotes_last_pretip_evaluation(engine, league):
    from nba_edge.db.research_schema import prop_evaluations
    from nba_edge.model.grading import grade_pending
    from nba_edge.model.run import run_slate
    _setup_recs(engine, league)
    run_slate(engine, now=league["now"] + timedelta(hours=1), cfg=FAST, dcfg=LOOSE, seed=9)
    _finish_game(engine, league)
    with engine.connect() as conn:
        n_eval = conn.execute(select(func.count()).select_from(prop_evaluations)).scalar()
        n_keys = conn.execute(select(func.count(func.distinct(prop_evaluations.c.quote_key)))).scalar()
    assert n_eval == 2 * n_keys
    assert grade_pending(engine)["graded"] == n_keys and grade_pending(engine)["graded"] == 0
    with engine.connect() as conn:
        graded_runs = {r[0] for r in conn.execute(select(prop_evaluations.c.run_id).join(prop_grades, prop_grades.c.evaluation_id == prop_evaluations.c.id))}
    assert len(graded_runs) == 1, "superseded evaluations stay ungraded (prunable)"
