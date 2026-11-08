"""Slate orchestration: projections for every rostered player of every not-yet-started game,
Monte Carlo distributions, and evaluation of every live FanDuel/Fanatics quote."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from statistics import median

import numpy as np
from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.engine import Engine

from ..db.research_schema import model_runs, nba_games, player_xref, projections, prop_evaluations, prop_grades
from ..db.schema import games as odds_games, odds_current, utcnow
from ..odds.markets import base_market_key
from ..odds.normalize import game_date_et
from ..research.ingest import latest_injuries
from ..research.names import match_player, norm_name
from ..research.teams import espn_abbr
from .config import MARKET_STATS, MODEL_VERSION, DecisionConfig, ModelConfig
from .history import load_history
from .paper import record_recommendations
from .pricing import evaluate, novig_two_way
from .project import GameCtx, player_params, team_adjust, team_roster
from .simulate import line_probs, simulate_player, summarize

log = logging.getLogger(__name__)


def slate_games(engine: Engine, date_et: str, now: datetime) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(nba_games).where(and_(
            nba_games.c.date_et == date_et, nba_games.c.season_type.in_([2, 3, 5]), nba_games.c.start_time > now)).order_by(nba_games.c.start_time))]


def live_graded_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(prop_grades).where(prop_grades.c.result.in_(["win", "loss"]))).scalar() or 0)


def _odds_events_for(engine: Engine, g: dict, provider: str) -> list[str]:
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(odds_games).where(and_(odds_games.c.provider == provider, or_(
            odds_games.c.espn_event_id == g["espn_event_id"], odds_games.c.game_date_et == g["date_et"]))))]
    out = []
    for r in rows:
        if r.get("espn_event_id") == g["espn_event_id"] or (espn_abbr(r["home_team"]) == g["home"] and espn_abbr(r["away_team"]) == g["away"]):
            out.append(r["provider_event_id"])
    return out


def run_slate(engine: Engine, *, now: datetime | None = None, date_et: str | None = None, cfg: ModelConfig = ModelConfig(),
              dcfg: DecisionConfig = DecisionConfig(), provider: str = "propline", seed: int | None = None,
              reference_books: tuple[str, ...] = ("draftkings", "pinnacle")) -> dict:
    now = now or utcnow()
    date_et = date_et or game_date_et(now)
    t0 = time.monotonic()
    with engine.begin() as conn:
        run_id = conn.execute(insert(model_runs).values(started_at=now, model_version=MODEL_VERSION, slate_date_et=date_et,
                                                        status="running", sims_per_player=cfg.n_sims)).inserted_primary_key[0]
    games = slate_games(engine, date_et, now)
    if not games:
        return _finish(engine, run_id, now, t0, status="no_games", detail=f"no not-yet-started games on {date_et} in nba_games (run ingest-espn first)")
    season = games[0]["season"]
    h = load_history(engine, now, season)
    inj = latest_injuries(engine, now)
    graded = live_graded_count(engine)
    rng = np.random.default_rng(seed)
    proj_rows, eval_rows, xref_rows = [], [], []
    sims: dict[str, object] = {}
    summs: dict[str, dict] = {}
    params_by_game: dict[str, list] = {}
    n_players = 0
    notes: list[str] = []
    for g in games:
        ctx = GameCtx(g["espn_event_id"], g["home"], g["away"], g["start_time"], g["date_et"])
        allp = []
        for team in (g["home"], g["away"]):
            plist = []
            for aid in team_roster(h, team, now):
                name = h.by_player[aid][0]["player_name"]
                injury = inj.get(aid) or inj.get(f"name:{name.lower()}")
                p = player_params(h, aid, team, ctx, injury, cfg)
                if p:
                    plist.append(p)
            team_adjust(plist, h, ctx, cfg)
            allp.extend(plist)
        params_by_game[g["espn_event_id"]] = allp
        for p in allp:
            n_players += 1
            summ = None
            if p.p_play > 0:
                sim = simulate_player(p, h.league, cfg, rng)
                sims[p.athlete_id] = sim
                summ = summarize(sim)
                summs[p.athlete_id] = summ
            for code in ("MIN", *MARKET_STATS):
                s = summ[code] if summ else {"mean": 0.0, "sd": 0.0, "p10": 0, "p25": 0, "median": 0, "p75": 0, "p90": 0}
                proj_rows.append(dict(run_id=run_id, espn_event_id=g["espn_event_id"], athlete_id=p.athlete_id, player_name=p.player_name,
                                      team=p.team, opponent=p.opponent, is_home=p.is_home, market_code=code, p_play=round(p.p_play, 4),
                                      mean=s["mean"], sd=s["sd"], p10=s["p10"], p25=s["p25"], median=s["median"], p75=s["p75"], p90=s["p90"],
                                      minutes_mean=round(p.min_mu, 2), games_this_season=p.games_this_season, games_sample=p.games_sample,
                                      injury_status=p.injury_status,
                                      notes=json.dumps({**p.notes, "research": p.research}, default=str) if code == "MIN" else None))
    # ---- odds evaluation
    books = set(dcfg.target_books) | set(reference_books)
    odds_as_of = None
    unmatched: dict[str, int] = {}
    for g in games:
        ev_ids = _odds_events_for(engine, g, provider)
        if not ev_ids:
            notes.append(f"{g['away']}@{g['home']}: no odds event")
            continue
        with engine.connect() as conn:
            quotes = [dict(r._mapping) for r in conn.execute(select(odds_current).where(and_(
                odds_current.c.provider == provider, odds_current.c.provider_event_id.in_(ev_ids), odds_current.c.market_status == "active",
                odds_current.c.bookmaker.in_(books), odds_current.c.commence_time > now)))]
        if not quotes:
            continue
        cands = {p.athlete_id: p.player_name for p in params_by_game[g["espn_event_id"]]}
        pmap = {p.athlete_id: p for p in params_by_game[g["espn_event_id"]]}
        pairs: dict[tuple, dict] = {}
        for q in quotes:
            if not q["is_alternate"]:
                pairs.setdefault((q["provider_event_id"], q["bookmaker"], q["market_key"], q["player_name"], q["line"]), {})[q["side"]] = q["decimal_odds"]
        overround: dict[str, float] = {}
        for bk in books:
            vals = [1 / v["over"] + 1 / v["under"] for k, v in pairs.items() if k[1] == bk and "over" in v and "under" in v]
            if vals:
                overround[bk] = median(vals)
        name_cache: dict[str, tuple] = {}
        # model-vs-market gap on each player's MAIN line (same book, same market), used to
        # check that an alternate's "edge" isn't just the model disagreeing with the centre
        main_gap: dict[tuple, float] = {}
        for q in quotes:
            if q["bookmaker"] not in dcfg.target_books:
                continue
            code = q["market_code"]
            if code not in MARKET_STATS or q["side"] not in ("over", "under") or q["line"] is None:
                continue
            key = norm_name(q["player_name"])
            if key not in name_cache:
                name_cache[key] = match_player(q["player_name"], cands)
                xref_rows.append(dict(provider=provider, provider_player_key=q.get("provider_player_id") or f"{key}|{g['espn_event_id']}",
                                      provider_name=q["player_name"], espn_athlete_id=name_cache[key][0], method=name_cache[key][1], matched_at=now))
            aid, how = name_cache[key]
            if not aid:
                unmatched[q["player_name"]] = unmatched.get(q["player_name"], 0) + 1
                continue
            sim = sims.get(aid)
            p = pmap[aid]
            seen = q.get("source_seen_at") or q.get("last_seen_at")
            odds_as_of = max(odds_as_of, seen) if odds_as_of and seen else (seen or odds_as_of)
            age = (now - seen).total_seconds() / 60 if seen else None
            if q["is_alternate"]:
                nv = (1 / q["decimal_odds"]) / overround[q["bookmaker"]] if q["bookmaker"] in overround else None
                method = "book_margin_approx" if nv is not None else "none"
            else:
                pr = pairs.get((q["provider_event_id"], q["bookmaker"], q["market_key"], q["player_name"], q["line"]), {})
                other = pr.get("under" if q["side"] == "over" else "over")
                nv = novig_two_way(q["decimal_odds"], other) if other else None
                method = "two_way" if other else "none"
            if sim is None:
                lp = {"p_win": 0.0, "p_push": 0.0, "p_unc": 1.0}
            else:
                lp = line_probs(sim, code, float(q["line"]), q["side"])
            gap_key = (q["bookmaker"], base_market_key(q["market_key"]), aid)
            if sim is not None and gap_key not in main_gap:
                for k2, v2 in pairs.items():
                    if k2[1] == q["bookmaker"] and k2[2] == base_market_key(q["market_key"]) and norm_name(k2[3]) == key and "over" in v2 and "under" in v2:
                        lpo = line_probs(sim, code, float(k2[4]), "over")
                        dec_o = lpo["p_win"] / max(1e-9, lpo["p_win"] + lpo["p_loss"])
                        main_gap[gap_key] = dec_o - novig_two_way(v2["over"], v2["under"])
                        break
            e = evaluate(p_win=lp["p_win"], p_push=lp["p_push"], p_unc=lp["p_unc"], p_play=p.p_play, decimal=q["decimal_odds"], novig=nv,
                         novig_method=method, is_alternate=bool(q["is_alternate"]), bookmaker=q["bookmaker"], stale=bool(q.get("stale")),
                         quote_age_min=age, games_sample=p.games_sample, games_this_season=p.games_this_season, live_graded=graded,
                         main_line_gap=main_gap.get(gap_key) if q["side"] == "over" else (-main_gap[gap_key] if gap_key in main_gap else None), cfg=dcfg)
            if sim is None:
                e.decision, e.reasons = "NO_BET", [{"gate": "projection", "pass": False, "note": "player projected not to play / no projection"}]
            eval_rows.append(dict(run_id=run_id, evaluated_at=now, quote_key=q["quote_key"], provider=provider, bookmaker=q["bookmaker"],
                                  provider_event_id=q["provider_event_id"], espn_event_id=g["espn_event_id"], athlete_id=aid,
                                  player_name=p.player_name, market_key=q["market_key"], market_code=code, is_alternate=bool(q["is_alternate"]),
                                  side=q["side"], line=q["line"], american_odds=q["american_odds"], decimal_odds=q["decimal_odds"], quote_seen_at=seen,
                                  implied_prob=e.implied_prob, novig_prob=e.novig_prob, novig_method=e.novig_method, model_prob=e.model_prob,
                                  model_push_prob=e.push_prob, model_prob_sd=e.prob_sd, model_prob_low=e.prob_low, p_play=e.p_play,
                                  fair_american=e.fair_american, ev=e.ev, ev_low=e.ev_low, edge_vs_novig=e.edge_vs_novig, confidence=e.confidence,
                                  decision=e.decision, reasons=json.dumps(e.reasons), model_version=MODEL_VERSION))
    with engine.begin() as conn:
        for chunk in (proj_rows[i:i + 2000] for i in range(0, len(proj_rows), 2000)):
            conn.execute(insert(projections), chunk)
        for chunk in (eval_rows[i:i + 2000] for i in range(0, len(eval_rows), 2000)):
            conn.execute(insert(prop_evaluations), chunk)
        # Paper ledger: freeze each newly qualified PAPER_CANDIDATE exactly as evaluated now.
        gmap = {g["espn_event_id"]: g for g in games}
        cands = []
        for r in eval_rows:
            if r["decision"] != "PAPER_CANDIDATE":
                continue
            g, p = gmap[r["espn_event_id"]], next(x for x in params_by_game[r["espn_event_id"]] if x.athlete_id == r["athlete_id"])
            s = (summs.get(r["athlete_id"]) or {}).get(r["market_code"]) or {}
            cands.append({**r, "slate_date_et": date_et, "commence_time": g["start_time"], "team": p.team, "opponent": p.opponent,
                          "projection_mean": s.get("mean"), "projection_median": s.get("median"), "projection_sd": s.get("sd"),
                          "minutes_mean": round(p.min_mu, 2)})
        n_paper_new = record_recommendations(conn, run_id=run_id, now=now, candidates=cands)
        for x in xref_rows:
            exists = conn.execute(select(player_xref.c.provider).where(and_(player_xref.c.provider == x["provider"],
                                  player_xref.c.provider_player_key == x["provider_player_key"]))).first()
            if exists:
                conn.execute(update(player_xref).where(and_(player_xref.c.provider == x["provider"], player_xref.c.provider_player_key == x["provider_player_key"])).values(**x))
            else:
                conn.execute(insert(player_xref).values(**x))
    if unmatched:
        notes.append(f"unmatched odds players ({len(unmatched)}): " + ", ".join(sorted(unmatched)[:15]))
    n_cand = sum(1 for r in eval_rows if r["decision"] == "PAPER_CANDIDATE")
    if n_cand:
        notes.append(f"paper ledger: {n_paper_new} new recommendation(s) recorded ({n_cand - n_paper_new} already on record)")
    return _finish(engine, run_id, now, t0, status="ok", n_games=len(games), n_players=n_players, n_projections=len(proj_rows),
                   n_evaluations=len(eval_rows), n_candidates=n_cand, odds_as_of=odds_as_of, detail="; ".join(notes) or None)


def _finish(engine: Engine, run_id: int, now: datetime, t0: float, **values) -> dict:
    values["finished_at"] = now + timedelta(seconds=time.monotonic() - t0)
    with engine.begin() as conn:
        conn.execute(update(model_runs).where(model_runs.c.id == run_id).values(**values))
        return dict(conn.execute(select(model_runs).where(model_runs.c.id == run_id)).mappings().one())
