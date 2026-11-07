"""Research ingestion, projection engine, pricing/decisions, grading, backtest, dashboard API —
all on SYNTHETIC data (tests/synthetic_league.py) or synthetic ESPN payloads."""
import json
import math
import threading
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import pytest
from sqlalchemy import insert, select, update

from nba_edge.db.research_schema import (injury_snapshots, model_runs, nba_games, nba_player_games, projections, prop_evaluations,
                                         prop_grades)
from nba_edge.db.schema import odds_snapshots, snapshot_runs
from nba_edge.model.config import DecisionConfig, ModelConfig
from nba_edge.model.history import load_history
from nba_edge.model.pricing import evaluate, ev_per_unit, fair_american, novig_two_way
from nba_edge.model.project import GameCtx, player_params, team_adjust, team_roster
from nba_edge.model.simulate import line_probs, simulate_player
from nba_edge.research.espn import EspnClient, parse_box, parse_injuries, parse_scoreboard
from nba_edge.research.ingest import effective_status, ingest_dates, latest_injuries
from nba_edge.research.names import match_player, norm_name
from nba_edge.research.teams import espn_abbr
from synthetic_league import SEASON, add_odds, build

START = datetime(2026, 9, 25, tzinfo=timezone.utc)
FAST = replace(ModelConfig(), n_sims=4000)


# ------------------------------------------------------------------ ESPN parsing / ingest
def _sb_event(eid, home, away, start, state="STATUS_FINAL", completed=True, stype=2):
    return {"id": eid, "date": start, "season": {"year": 2027, "type": stype}, "status": {"type": {"name": state, "completed": completed}},
            "competitions": [{"competitors": [{"homeAway": "home", "team": {"abbreviation": home}, "score": "101"},
                                              {"homeAway": "away", "team": {"abbreviation": away}, "score": "99"}]}]}


def _summary(home, away, ot=False):
    labels = ["MIN", "PTS", "FG", "3PT", "FT", "REB", "AST", "TO", "STL", "BLK", "OREB", "DREB", "PF", "+/-"]
    def ath(i, mins, team, dnp=False):
        return {"athlete": {"id": f"{team}{i}", "displayName": f"Synth {team} {i}", "position": {"abbreviation": "G"}}, "starter": i < 5,
                "didNotPlay": dnp, "reason": "DNP-COACH'S DECISION" if dnp else "COACH'S DECISION",
                "stats": [] if dnp else [str(mins), "10", "4-9", "1-3", "1-2", "5", "3", "2", "1", "0", "1", "4", "2", "+3"]}
    per = 53 if ot else 48
    return {"boxscore": {"players": [{"team": {"abbreviation": t}, "statistics": [{"labels": labels, "athletes":
            [ath(i, per * 5 // 5 if i < 5 else 0, t) for i in range(5)] + [ath(9, 0, t, dnp=True)]}]} for t in (home, away)]}}


def test_parse_scoreboard_box_and_team_rows():
    g = parse_scoreboard({"events": [_sb_event("1", "GS", "NY", "2026-11-07T00:30Z")]})[0]
    assert (g.home, g.away, g.season, g.completed, g.date_et) == ("GS", "NY", 2027, True, "2026-11-06")
    players, teams = parse_box(_summary("GS", "NY"), g)
    assert len(players) == 12 and sum(p["played"] for p in players) == 10
    dnp = [p for p in players if not p["played"]][0]
    assert dnp["minutes"] == 0 and dnp["pts"] == 0 and dnp["dnp_reason"].startswith("DNP")
    active = [p for p in players if p["played"]][0]
    assert active["dnp_reason"] is None, "stale 'reason' on active players is dropped"
    assert (active["fgm"], active["fga"], active["fg3m"], active["fg3a"]) == (4, 9, 1, 3)
    t = {x["team"]: x for x in teams}
    assert t["GS"]["minutes"] == 48.0 and t["GS"]["opp_pts"] == t["NY"]["pts"] == 50
    assert t["GS"]["possessions"] == pytest.approx(5 * (9 + 0.44 * 2 - 1 + 2))
    _, teams_ot = parse_box(_summary("GS", "NY", ot=True), g)
    assert teams_ot[0]["minutes"] == 53.0, "overtime game length detected from team minutes"


def test_injury_parsing_and_effective_status():
    now = datetime(2026, 11, 7, 18, tzinfo=timezone.utc)
    data = {"injuries": [{"displayName": "Golden State Warriors", "injuries": [
        {"status": "Day-To-Day", "shortComment": "X is questionable for Friday's game.", "date": "2026-11-07T15:00Z",
         "athlete": {"displayName": "Synth Guard", "links": [{"href": "https://www.espn.com/nba/player/_/id/123/synth-guard"}]}}]}]}
    r = parse_injuries(data, espn_abbr)[0]
    assert (r.athlete_id, r.team, r.status) == ("123", "GS", "Day-To-Day")
    assert effective_status(r.status, r.detail, r.reported_at, now) == "Questionable"
    assert effective_status("Day-To-Day", "Y has been ruled out.", now - timedelta(hours=2), now) == "Out"
    assert effective_status("Day-To-Day", "Y has been ruled out.", now - timedelta(days=3), now) == "Day-To-Day", "stale comments are ignored"
    assert effective_status("Out", "will play tonight", now, now) == "Available"


def test_ingest_dates_is_idempotent_and_survives_missing_box(engine):
    calls = {"summary": 0}

    def handler(req: httpx.Request):
        if req.url.path.endswith("/scoreboard"):
            d = req.url.params["dates"]
            evs = [_sb_event("A" + d, "GS", "NY", "2026-11-07T00:30Z"), _sb_event("B" + d, "BOS", "MIA", "2026-11-07T00:30Z"),
                   _sb_event("P" + d, "LAL", "LAC", "2026-11-07T00:30Z", stype=1), _sb_event("U" + d, "DEN", "IND", "2026-11-08T00:30Z", "STATUS_SCHEDULED", False),
                   _sb_event("S" + d, "STRIPES", "WORLD", "2026-11-07T00:30Z")]
            return httpx.Response(200, json={"events": evs})
        calls["summary"] += 1
        if req.url.params["event"].startswith("B"):
            return httpx.Response(404, json={"code": 404})
        return httpx.Response(200, json=_summary("GS", "NY"))
    c = EspnClient(transport=httpx.MockTransport(handler), sleep=lambda s: None, min_interval_s=0)
    d = datetime(2026, 11, 6).date()
    r1 = ingest_dates(engine, c, d, d)
    assert r1["status"] == "partial" and r1["boxes"] == 1 and r1["errors"] == 1, "404 box recorded, run continues"
    assert calls["summary"] == 2, "preseason, unfinished and non-NBA-team (All-Star) games are not fetched"
    assert any("non-NBA-team" in n for n in r1["notes"])
    r2 = ingest_dates(engine, c, d, d)
    assert r2["boxes"] == 0 and calls["summary"] == 3, "ingested box is not refetched (the failed one is retried)"
    with engine.connect() as conn:
        assert conn.execute(select(nba_player_games)).all().__len__() == 12
        assert not conn.execute(select(nba_games).where(nba_games.c.home == "STRIPES")).first()


def test_name_and_team_matching():
    cands = {"1": "Jaren Jackson Jr.", "2": "Nikola Jokić", "3": "Jalen Williams", "4": "Jaylin Williams"}
    assert match_player("Jaren Jackson", cands) == ("1", "exact")
    assert match_player("Nikola Jokic", cands) == ("2", "exact")
    assert match_player("J. Williams", cands)[0] is None, "ambiguous initials are never guessed"
    assert match_player("Someone Else", cands) == (None, "unmatched")
    assert norm_name("D'Angelo Russell") == norm_name("DAngelo Russell")
    assert [espn_abbr(x) for x in ("Golden State Warriors", "GSW", "Knicks", "NYK", "LA Clippers", "Utah Jazz")] == ["GS", "GS", "NY", "NY", "LAC", "UTAH"]


# ------------------------------------------------------------------ model core
@pytest.fixture
def league(engine):
    return build(engine, START)


def test_history_no_leakage_and_noise_estimates(engine, league):
    h = load_history(engine, league["now"], SEASON)
    assert all(g["start_time"] < league["now"] for gs in h.by_player.values() for g in gs)
    early = load_history(engine, START + timedelta(days=10), SEASON)
    assert max(len(v) for v in early.by_player.values()) <= 10
    lg = h.league
    assert all(0.99 <= v < 1.25 for v in lg["phi"].values()), "synthetic Poisson data -> phi ~ 1"
    assert np.allclose(np.diag(lg["corr"]), 1.0)
    assert lg["team_pace"]["AAA"] > lg["team_pace"]["CCC"]
    assert set(lg["min_ratio"]) == {"starter", "rotation", "bench"}


def test_player_params_recover_rates_and_shrink_small_samples(engine, league):
    h = load_history(engine, league["now"], SEASON)
    s = league["slate"]
    ctx = GameCtx(s["espn_event_id"], s["home"], s["away"], s["start_time"], s["date_et"])
    p = player_params(h, "AAA0", "AAA", ctx, None, FAST)
    assert p.rate_mean("pts") == pytest.approx(0.55, rel=0.12)
    assert p.min_mu == pytest.approx(32, abs=2.5) and p.p_play > 0.9
    tiny = load_history(engine, START + timedelta(days=4), SEASON)
    p3 = player_params(tiny, "AAA0", "AAA", ctx, None, FAST)
    pos_rate = tiny.league["pos_rate"]["G"]["pts"]
    raw = sum(g["pts"] for g in tiny.by_player["AAA0"]) / sum(g["minutes"] for g in tiny.by_player["AAA0"])
    assert min(raw, pos_rate) - 1e-9 <= p3.rate_mean("pts") <= max(raw, pos_rate) + 1e-9, "small samples are shrunk toward the prior"
    out = player_params(h, "AAA0", "AAA", ctx, {"effective": "Out"}, FAST)
    assert out.p_play == 0.0
    assert player_params(h, "nobody", "AAA", ctx, None, FAST) is None


def test_known_absence_redistributes_minutes_within_cap(engine, league):
    h = load_history(engine, league["now"], SEASON)
    s = league["slate"]
    ctx = GameCtx(s["espn_event_id"], s["home"], s["away"], s["start_time"], s["date_et"])

    def team(inj):
        ps = [player_params(h, a, "AAA", ctx, inj.get(a), FAST) for a in team_roster(h, "AAA", league["now"])]
        team_adjust(ps, h, ctx, FAST)
        return {p.athlete_id: p for p in ps}
    base = team({})
    hurt = team({"AAA0": {"effective": "Out"}})
    assert base["AAA1"].notes["team"]["minutes_scale_to_240"] == 1.0, "no known absence -> no rescale (v1.2)"
    up = hurt["AAA1"].min_mu / base["AAA1"].min_mu
    assert 1.0 < up <= 1.10 + 1e-9
    assert hurt["AAA1"].factor["pts"] > base["AAA1"].factor["pts"], "usage boost for teammates"


def test_simulation_combos_and_line_probabilities(engine, league):
    h = load_history(engine, league["now"], SEASON)
    s = league["slate"]
    ctx = GameCtx(s["espn_event_id"], s["home"], s["away"], s["start_time"], s["date_et"])
    p = player_params(h, "AAA0", "AAA", ctx, None, FAST)
    sim = simulate_player(p, h.league, ModelConfig(), np.random.default_rng(0))
    assert len(sim.minutes) == 10_000, "10,000 simulations per player by default"
    assert np.array_equal(sim.stats["PRA"], sim.stats["PTS"] + sim.stats["REB"] + sim.stats["AST"])
    expected = p.rate_mean("pts") * p.min_mu * p.factor["pts"]
    assert sim.stats["PTS"].mean() == pytest.approx(expected, rel=0.08)
    lp = line_probs(sim, "PTS", 17.5, "over")
    assert lp["p_win"] == pytest.approx((sim.stats["PTS"] > 17.5).mean()) and lp["p_push"] == 0 and lp["p_unc"] >= 0
    lp2 = line_probs(sim, "PTS", 17.0, "under")
    assert lp2["p_push"] == pytest.approx((sim.stats["PTS"] == 17).mean()) and lp2["p_win"] + lp2["p_push"] + lp2["p_loss"] == pytest.approx(1)


# ------------------------------------------------------------------ pricing / decisions
def _ev(**kw):
    base = dict(p_win=0.6, p_push=0.0, p_unc=0.01, p_play=0.98, decimal=1.95, novig=0.52, novig_method="two_way", is_alternate=False,
                bookmaker="fanduel", stale=False, quote_age_min=3.0, games_sample=40, games_this_season=10, live_graded=0)
    return evaluate(**{**base, **kw})


def test_ev_fair_odds_and_novig_math():
    assert ev_per_unit(1.0, 0.5, 0.5, 2.0) == pytest.approx(0.0)
    assert ev_per_unit(0.5, 0.6, 0.4, 2.0) == pytest.approx(0.5 * 0.2), "void when not playing scales EV by p_play"
    assert ev_per_unit(1.0, 0.5, 0.4, 2.0) == pytest.approx(0.1), "pushes return the stake"
    assert fair_american(0.5, 0.5) == 100 and fair_american(0.8, 0.2) == -400
    assert novig_two_way(1.909, 1.909) == pytest.approx(0.5)


def test_decision_gates_default_to_no_bet():
    c = _ev()
    assert c.decision == "PAPER_CANDIDATE" and any("paper-trade only" in r.get("note", "") for r in c.reasons)
    assert _ev(p_win=0.5).decision == "NO_BET"
    assert _ev(novig=0.40).decision == "NO_BET", "a large model/market gap is treated as model error"
    assert _ev(stale=True).decision == "WATCH"
    assert _ev(quote_age_min=None).decision == "WATCH"
    assert _ev(p_play=0.6).decision == "WATCH"
    assert _ev(games_sample=5).decision == "WATCH"
    assert _ev(bookmaker="draftkings").decision == "WATCH"
    assert _ev(p_unc=0.08).decision != "PAPER_CANDIDATE"
    alt = dict(is_alternate=True, novig_method="book_margin_approx")
    assert _ev(decimal=4.5, p_win=0.30, novig=0.20, **alt).decision == "NO_BET", "longshot alternates are never candidates"
    assert _ev(p_win=0.64, novig=0.55, main_line_gap=0.10, **alt).decision != "PAPER_CANDIDATE", "alt needs main-line agreement"
    assert _ev(p_win=0.64, novig=0.55, main_line_gap=0.01, **alt).decision == "PAPER_CANDIDATE"


# ------------------------------------------------------------------ slate run, grading, retention, backtest
def _odds_for(league):
    return [("fanduel", "player_points", "PTS", "Synth AAA Player0", "over", 17.5, -110, False),
            ("fanduel", "player_points", "PTS", "Synth AAA Player0", "under", 17.5, -110, False),
            ("fanatics", "player_points_milestone", "PTS", "Synth AAA Player0", "over", 19.5, 150, True),
            ("fanduel", "player_rebounds", "REB", "Synth BBB Player4", "over", 8.5, -120, False),
            ("fanduel", "player_rebounds", "REB", "Synth BBB Player4", "under", 8.5, -105, False),
            ("draftkings", "player_points", "PTS", "Synth AAA Player0", "over", 17.5, -105, False),
            ("fanduel", "player_assists", "AST", "Unknown Person", "over", 2.5, -110, False)]


def test_run_slate_projects_everyone_and_evaluates_target_books(engine, league):
    from nba_edge.model.run import run_slate
    add_odds(engine, league["now"], league["slate"], _odds_for(league))
    with engine.begin() as conn:
        conn.execute(insert(injury_snapshots).values(captured_at=league["now"], source="espn", athlete_id="BBB7", player_name="Synth BBB Player7",
                                                     team="BBB", status="Out", detail=None, reported_at=league["now"]))
    r = run_slate(engine, now=league["now"], cfg=FAST, seed=1)
    assert r["status"] == "ok" and r["n_games"] == 1 and r["n_players"] == 16, "every rostered player incl. bench"
    assert r["n_evaluations"] == 5, "FanDuel/Fanatics quotes only; DraftKings is a reference book; unknown player unmatched"
    assert "Unknown Person" in r["detail"]
    with engine.connect() as conn:
        evs = [dict(x._mapping) for x in conn.execute(select(prop_evaluations))]
        out = conn.execute(select(projections).where(projections.c.athlete_id == "BBB7", projections.c.market_code == "PTS")).mappings().one()
    assert out["p_play"] == 0.0 and out["injury_status"] == "Out"
    by = {(e["bookmaker"], e["market_code"], e["side"]): e for e in evs}
    o, u = by[("fanduel", "PTS", "over")], by[("fanduel", "PTS", "under")]
    assert o["novig_method"] == "two_way" and o["novig_prob"] == pytest.approx(0.5)
    assert o["model_prob"] + u["model_prob"] == pytest.approx(1.0, abs=1e-9)
    assert by[("fanatics", "PTS", "over")]["novig_method"] in ("book_margin_approx", "none")
    assert all(e["decision"] in ("NO_BET", "WATCH", "PAPER_CANDIDATE") for e in evs)


def test_grading_real_outcomes_and_clv(engine, league):
    from nba_edge.model.grading import grade_pending, live_performance
    from nba_edge.model.run import run_slate
    add_odds(engine, league["now"], league["slate"], _odds_for(league))
    run_slate(engine, now=league["now"], cfg=FAST, seed=2)
    s = league["slate"]
    with engine.begin() as conn:  # the game happens: AAA0 scores 20, BBB4 did not play
        conn.execute(update(nba_games).where(nba_games.c.espn_event_id == s["espn_event_id"]).values(completed=True, box_ingested=True))
        base = dict(espn_event_id=s["espn_event_id"], position="G", opponent="BBB", is_home=True, starter=True, dnp_reason=None, season=SEASON,
                    season_type=2, date_et=s["date_et"], start_time=s["start_time"], reb=5, ast=3, fg3m=1, stl=1, blk=0, tov=2)
        conn.execute(insert(nba_player_games), [{**base, "athlete_id": "AAA0", "player_name": "Synth AAA Player0", "team": "AAA", "played": True, "minutes": 33, "pts": 20},
                                                {**base, "athlete_id": "BBB4", "player_name": "Synth BBB Player4", "team": "BBB", "opponent": "AAA", "is_home": False,
                                                 "played": False, "minutes": 0, "pts": 0}])
        qk = conn.execute(select(prop_evaluations.c.quote_key).where(prop_evaluations.c.bookmaker == "fanduel", prop_evaluations.c.market_code == "PTS",
                                                                     prop_evaluations.c.side == "over")).scalar()
        rid = conn.execute(insert(snapshot_runs).values(reason="closing", started_at=s["start_time"] - timedelta(minutes=5), status="ok")).inserted_primary_key[0]
        conn.execute(insert(odds_snapshots).values(run_id=rid, captured_at=s["start_time"] - timedelta(minutes=5), snapshot_reason="closing",
                                                   change_type="price_change", quote_key=qk, provider="propline", sport_key="basketball_nba",
                                                   provider_event_id="pl-1", bookmaker="fanduel", market_key="player_points", is_alternate=False,
                                                   player_name="Synth AAA Player0", side="over", line=17.5, american_odds=-130, decimal_odds=1 + 100 / 130,
                                                   market_status="active"))
    g = grade_pending(engine)
    assert g["graded"] == 5 and g["by_result"]["void"] == 2 and g["by_result"]["win"] + g["by_result"]["loss"] == 3
    with engine.connect() as conn:
        res = {(r.bookmaker, r.side, r.market_code): r.result for r in conn.execute(
            select(prop_evaluations.c.bookmaker, prop_evaluations.c.side, prop_evaluations.c.market_code, prop_grades.c.result)
            .join(prop_grades, prop_grades.c.evaluation_id == prop_evaluations.c.id))}
    assert res[("fanduel", "over", "PTS")] == "win" and res[("fanduel", "under", "PTS")] == "loss" and res[("fanatics", "over", "PTS")] == "win"
    with engine.connect() as conn:
        clv = conn.execute(select(prop_grades.c.clv_prob, prop_grades.c.closing_american).join(prop_evaluations, prop_evaluations.c.id == prop_grades.c.evaluation_id)
                           .where(prop_evaluations.c.quote_key == qk)).first()
    assert clv.closing_american == -130 and clv.clv_prob == pytest.approx(130 / 230 - 110 / 210), "closing moved toward the over: positive CLV"
    assert grade_pending(engine)["graded"] == 0, "graded once"
    perf = live_performance(engine)
    assert perf["n_decided"] == 3 and perf["n_void"] == 2 and perf["model"]["n"] == 3


def test_backtest_is_walk_forward_and_labeled_simulated(engine, league):
    from nba_edge.model.backtest import LINE_METHOD, run_backtest
    d = (START + timedelta(days=30)).date()
    r = run_backtest(engine, d, d + timedelta(days=4), every_n_days=2, n_sims=1000, cfg=FAST)
    assert r["rows"] > 0 and "SIMULATED" in LINE_METHOD and r["summary"]["line_method"] == LINE_METHOD
    m = r["summary"]["markets"]["PTS"]
    assert m["raw_L10_hit_rate_baseline"] is not None and len(m["pit_deciles"]) == 10
    # leakage check: changing the evaluated day's actual box must not change its projections
    with engine.begin() as conn:
        conn.execute(update(nba_player_games).where(nba_player_games.c.date_et == d.isoformat()).values(pts=200))
    r2 = run_backtest(engine, d, d, every_n_days=1, n_sims=1000, cfg=FAST)
    r1 = run_backtest(engine, d, d, every_n_days=1, n_sims=1000, cfg=FAST)
    assert r1["summary"]["markets"]["PTS"]["model"]["n"] == r2["summary"]["markets"]["PTS"]["model"]["n"]


def test_retention_keeps_last_pretip_run(engine, league):
    from nba_edge.model.retention import prune_evaluations
    from nba_edge.model.run import run_slate
    add_odds(engine, league["now"], league["slate"], _odds_for(league))
    old = league["now"] - timedelta(days=30)
    run_slate(engine, now=league["now"] - timedelta(hours=3), cfg=FAST, seed=3)
    run_slate(engine, now=league["now"], cfg=FAST, seed=4)
    with engine.begin() as conn:
        conn.execute(update(model_runs).values(started_at=old))
        for (eid, t) in conn.execute(select(prop_evaluations.c.id, prop_evaluations.c.evaluated_at)).all():
            conn.execute(update(prop_evaluations).where(prop_evaluations.c.id == eid).values(evaluated_at=t - timedelta(days=30)))
        for (gid, t) in conn.execute(select(nba_games.c.espn_event_id, nba_games.c.start_time)).all():
            conn.execute(update(nba_games).where(nba_games.c.espn_event_id == gid).values(start_time=t - timedelta(days=30)))
    r = prune_evaluations(engine, keep_days=14)
    assert r["runs_considered"] == 2 and r["runs_retained_for_calibration"] == 1 and r["evaluations_deleted"] == 5


# ------------------------------------------------------------------ dashboard
def test_dashboard_api_and_static(engine, league):
    from nba_edge.dashboard.server import serve
    from nba_edge.model.run import run_slate
    add_odds(engine, league["now"], league["slate"], _odds_for(league))
    run_slate(engine, now=league["now"], cfg=FAST, seed=5)
    httpd = serve(engine, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        get = lambda p: json.loads(urllib.request.urlopen(base + p, timeout=10).read())  # noqa: E731
        st = get("/api/status")
        assert "warnings" in st and st["model_run"]["n_players"] == 16
        pr = get("/api/props?decision=NO_BET")
        assert pr["rows"] and all(r["decision"] == "NO_BET" for r in pr["rows"])
        assert "no_bet" in get("/api/props")
        pj = get("/api/projections")
        assert len(pj["players"]) == 16 and "PRA" in pj["players"][0]
        pl = get("/api/player/AAA0")
        assert pl["logs"] and pl["projection_notes"]["model_version"] and "NOT used as probabilities" in pl["hit_rate_note"]
        perf = get("/api/performance")
        assert "REAL" in perf["live"]["label"]
        html = urllib.request.urlopen(base + "/", timeout=10).read().decode()
        assert "NBA Prop Research" in html
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/../../nba_edge/config.py", timeout=10)
    finally:
        httpd.shutdown()


def test_dashboard_js_parses():
    import os
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nba_edge", "dashboard", "static", "app.js")
    assert subprocess.run([node, "--check", js], capture_output=True).returncode == 0
