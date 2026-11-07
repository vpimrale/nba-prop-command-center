"""Walk-forward PROJECTION backtest on historical ESPN box scores.

!!! SIMULATED LINES — NOT BETTING RESULTS !!!
No historical sportsbook prices exist in this project yet (the odds snapshot archive starts
2026-11-07). So this backtest measures how well the model's DISTRIBUTIONS match what
players actually did, using lines constructed from information available before each game
(the player's L10 mean of that stat, floored to x.5). It reports calibration, Brier, log
loss and PIT. It says nothing about profitability against real prices.

Other honest limits: historical injury reports and pre-game lineups are not available, so
the model runs WITHOUT injury inputs and is evaluated only on players who actually played
(props are void otherwise). Strictly no leakage: every projection for date D uses only box
scores whose games started before the first tip-off on D.

Baseline for contrast: the raw L10 hit rate of the same line, used as if it were a
probability — the practice this project explicitly avoids.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta

import numpy as np
from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.engine import Engine

from ..db.research_schema import backtest_rows, backtest_runs, nba_games, nba_player_games
from ..db.schema import utcnow
from .config import MARKET_STATS, MODEL_VERSION, STAT_CODE, ModelConfig
from .history import load_history
from .metrics import pit_hist, summarize_binary
from .project import GameCtx, player_params, team_adjust, team_roster
from .simulate import simulate_player

LINE_METHOD = "SIMULATED: floor(prior L10 mean) + 0.5 — not a sportsbook line"
CODE_TO_COLS = {code: [s for s in comps] for code, comps in MARKET_STATS.items()}


def _actual(row: dict, code: str) -> float:
    return float(sum(row[s] or 0 for s in CODE_TO_COLS[code]))


def run_backtest(engine: Engine, date_from: date, date_to: date, *, every_n_days: int = 3, n_sims: int = 2000,
                 cfg: ModelConfig = ModelConfig(), seed: int = 11, max_dates: int | None = None) -> dict:
    rng = np.random.default_rng(seed)
    with engine.connect() as conn:
        days = sorted({r[0] for r in conn.execute(select(nba_games.c.date_et).where(and_(
            nba_games.c.date_et >= date_from.isoformat(), nba_games.c.date_et <= date_to.isoformat(),
            nba_games.c.season_type == 2, nba_games.c.box_ingested.is_(True))))})
    days = days[::every_n_days]
    if max_dates:
        days = days[:max_dates]
    with engine.begin() as conn:
        bt_id = conn.execute(insert(backtest_runs).values(created_at=utcnow(), model_version=MODEL_VERSION, date_from=date_from.isoformat(),
                                                          date_to=date_to.isoformat(), line_method=LINE_METHOD)).inserted_primary_key[0]
    rows_out = []
    for d in days:
        with engine.connect() as conn:
            day_games = [dict(r._mapping) for r in conn.execute(select(nba_games).where(and_(nba_games.c.date_et == d, nba_games.c.season_type == 2,
                                                                                         nba_games.c.box_ingested.is_(True))))]
            if not day_games:
                continue
            actual = {(r.espn_event_id, r.athlete_id): dict(r._mapping) for r in conn.execute(select(nba_player_games).where(and_(
                nba_player_games.c.date_et == d, nba_player_games.c.played.is_(True))))}
        cutoff = min(g["start_time"] for g in day_games) - timedelta(minutes=1)
        h = load_history(engine, cutoff, day_games[0]["season"])
        for g in day_games:
            ctx = GameCtx(g["espn_event_id"], g["home"], g["away"], g["start_time"], g["date_et"])
            for team in (g["home"], g["away"]):
                plist = [p for aid in team_roster(h, team, cutoff) if (p := player_params(h, aid, team, ctx, None, cfg))]
                team_adjust(plist, h, ctx, cfg)
                for p in plist:
                    act = actual.get((g["espn_event_id"], p.athlete_id))
                    if not act:   # did not play -> a prop would be void; not scored
                        continue
                    sim = simulate_player(p, h.league, cfg, rng, n_sims=n_sims)
                    hist = [x for x in h.by_player[p.athlete_id] if x["played"]][:10]
                    if len(hist) < 5:
                        continue
                    for code in MARKET_STATS:
                        vals = [_actual(x, code) for x in hist]
                        line = math.floor(float(np.mean(vals))) + 0.5
                        x = sim.stats[code]
                        a = _actual(act, code)
                        rows_out.append(dict(backtest_id=bt_id, date_et=d, athlete_id=p.athlete_id, market_code=code, line=line,
                                             p_over=float((x > line).mean()), p_push=float((x == line).mean()), mean=float(x.mean()), actual=a,
                                             pit=float((x < a).mean() + 0.5 * (x == a).mean()),
                                             _hit_rate=sum(1 for v in vals if v > line) / len(vals)))
    summary = summarize_backtest(rows_out)
    with engine.begin() as conn:
        for i in range(0, len(rows_out), 5000):
            conn.execute(insert(backtest_rows), [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows_out[i:i + 5000]])
        conn.execute(update(backtest_runs).where(backtest_runs.c.id == bt_id).values(n_rows=len(rows_out), summary=json.dumps(summary)))
    return {"backtest_id": bt_id, "dates": len(days), "rows": len(rows_out), "summary": summary}


def summarize_backtest(rows: list[dict]) -> dict:
    out = {"line_method": LINE_METHOD, "markets": {}}
    for code in MARKET_STATS:
        rs = [r for r in rows if r["market_code"] == code]
        if not rs:
            continue
        ys = [int(r["actual"] > r["line"]) for r in rs]
        model = summarize_binary([r["p_over"] for r in rs], ys)
        hr = [min(0.98, max(0.02, r["_hit_rate"])) for r in rs] if "_hit_rate" in rs[0] else None
        out["markets"][code] = {
            "n": len(rs), "mae_mean": round(float(np.mean([abs(r["mean"] - r["actual"]) for r in rs])), 3),
            "bias_mean_minus_actual": round(float(np.mean([r["mean"] - r["actual"] for r in rs])), 3),
            "model": model, "raw_L10_hit_rate_baseline": summarize_binary(hr, ys) if hr else None,
            "pit_deciles": pit_hist([r["pit"] for r in rs if r["pit"] is not None]),
        }
    allr = rows
    if allr:
        ys = [int(r["actual"] > r["line"]) for r in allr]
        out["all_markets"] = {"model": summarize_binary([r["p_over"] for r in allr], ys),
                              "raw_L10_hit_rate_baseline": summarize_binary([min(0.98, max(0.02, r["_hit_rate"])) for r in allr], ys)
                              if "_hit_rate" in allr[0] else None}
    return out


def latest_backtest(engine: Engine) -> dict | None:
    with engine.connect() as conn:
        r = conn.execute(select(backtest_runs).order_by(backtest_runs.c.id.desc()).limit(1)).mappings().first()
    if not r:
        return None
    d = dict(r)
    d["summary"] = json.loads(d["summary"]) if d["summary"] else None
    return d


__all__ = ["run_backtest", "latest_backtest", "LINE_METHOD", "STAT_CODE", "func", "datetime"]
