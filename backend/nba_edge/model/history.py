"""Load box-score history (strictly before a cutoff — no leakage) and estimate league
context from it: pace, opponent allowance factors, home edge, dispersion, correlations,
positional prior rates."""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from ..db.research_schema import nba_player_games, nba_team_games
from .config import ModelConfig, STATS

REG_TYPES = (2, 5)  # regular season + play-in (postseason/preseason excluded from rates)


def pos_group(pos: str | None) -> str:
    p = (pos or "").upper()
    if p.startswith("C"):
        return "C"
    if p.endswith("G") or p == "G":
        return "G"
    return "F"


@dataclass
class History:
    cutoff: datetime
    season: int
    by_player: dict[str, list[dict]]          # athlete_id -> games (newest first), both seasons, regular only
    team_games: list[dict]                    # current-season team rows (fallback: previous)
    team_stats: dict[tuple[str, str], dict]   # (event, team) -> summed player stats incl. stl/blk
    league: dict = field(default_factory=dict)


def load_history(engine: Engine, cutoff: datetime, season: int) -> History:
    c = nba_player_games.c
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(nba_player_games).where(and_(
            c.start_time < cutoff, c.season.in_([season, season - 1]), c.season_type.in_(REG_TYPES))).order_by(c.start_time.desc()))]
        tg = [dict(r._mapping) for r in conn.execute(select(nba_team_games).where(and_(
            nba_team_games.c.season.in_([season, season - 1]), nba_team_games.c.season_type.in_(REG_TYPES))))]
    by_player: dict[str, list[dict]] = defaultdict(list)
    team_stats: dict[tuple[str, str], dict] = defaultdict(lambda: {s: 0 for s in STATS})
    event_time: dict[str, datetime] = {}
    for r in rows:
        by_player[r["athlete_id"]].append(r)
        event_time[r["espn_event_id"]] = r["start_time"]
        if r["played"]:
            ts = team_stats[(r["espn_event_id"], r["team"])]
            for s in STATS:
                ts[s] += r[s] or 0
    tg = [t for t in tg if t["espn_event_id"] in event_time]
    cur = [t for t in tg if t["season"] == season]
    h = History(cutoff, season, dict(by_player), cur if len(cur) >= 60 else tg, dict(team_stats))
    h.league = league_context(h)
    return h


def league_context(h: History) -> dict:
    tg = h.team_games
    if not tg:
        return {"pace": 100.0, "team_pace": {}, "opp_factor": {}, "home_factor": 1.0, "net": {}, "n_team_games": 0}
    pace_vals = [t["possessions"] / t["minutes"] * 48 for t in tg if t["possessions"] and t["minutes"]]
    league_pace = float(np.mean(pace_vals)) if pace_vals else 100.0
    per_team: dict[str, list] = defaultdict(list)
    allowed: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    allowed_poss: dict[str, float] = defaultdict(float)
    allowed_n: dict[str, int] = defaultdict(int)
    tot = defaultdict(float)
    tot_poss = 0.0
    net: dict[str, list] = defaultdict(list)
    home_pts, away_pts = [], []
    for t in tg:
        if not t["possessions"] or not t["minutes"]:
            continue
        per_team[t["team"]].append(t["possessions"] / t["minutes"] * 48)
        st = h.team_stats.get((t["espn_event_id"], t["team"]))
        if st:  # what THIS team recorded is what its OPPONENT allowed
            for s in STATS:
                allowed[t["opponent"]][s] += st[s]
                tot[s] += st[s]
            allowed_poss[t["opponent"]] += t["possessions"]
            allowed_n[t["opponent"]] += 1
            tot_poss += t["possessions"]
        if t["opp_pts"] is not None:
            net[t["team"]].append((t["pts"] - t["opp_pts"]) / t["possessions"] * 100)
        (home_pts if t["is_home"] else away_pts).append(t["pts"])
    cfg = ModelConfig()
    team_pace = {k: (len(v) * float(np.mean(v)) + cfg.pace_k_games * league_pace) / (len(v) + cfg.pace_k_games) for k, v in per_team.items()}
    league_rate = {s: tot[s] / tot_poss for s in STATS} if tot_poss else {}
    opp_factor: dict[str, dict] = {}
    for team, d in allowed.items():
        n = allowed_n[team]
        w = n / (n + cfg.opp_k_games)
        opp_factor[team] = {s: 1 + w * ((d[s] / allowed_poss[team]) / league_rate[s] - 1) if league_rate.get(s) else 1.0 for s in STATS}
    home_factor = (float(np.mean(home_pts)) / float(np.mean(away_pts))) if home_pts and away_pts else 1.0
    net_rt = {k: len(v) * float(np.mean(v)) / (len(v) + cfg.pace_k_games) for k, v in net.items()}
    return {"pace": league_pace, "team_pace": team_pace, "opp_factor": opp_factor, "home_factor": home_factor, "net": net_rt,
            "n_team_games": len(tg), **estimate_noise(h)}


def estimate_noise(h: History) -> dict:
    """Per-stat overdispersion phi (Var = phi * mean, given minutes), residual correlation
    matrix across stats, and positional per-minute prior rates — all from the data.
    Two passes: per-player rates -> pooled phi -> standardized residuals -> correlation."""
    pos_tot: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    samples = []
    for games in h.by_player.values():
        played = [g for g in games if g["played"] and g["minutes"] >= 5]
        if not played:
            continue
        g0 = pos_group(played[0]["position"])
        for g in played:
            for s in STATS:
                pos_tot[g0][s] += g[s] or 0
            pos_tot[g0]["min"] += g["minutes"]
        if len(played) >= 10:
            mins = sum(g["minutes"] for g in played)
            samples.append(({s: sum(g[s] or 0 for g in played) / mins for s in STATS}, played))
    num = defaultdict(float)
    den = defaultdict(float)
    for rate, played in samples:
        for g in played:
            for s in STATS:
                mu = rate[s] * g["minutes"]
                if mu > 0:
                    num[s] += ((g[s] or 0) - mu) ** 2
                    den[s] += mu
    phi = {s: (max(1.0, num[s] / den[s]) if den[s] else 1.0) for s in STATS}
    resid = [[((g[s] or 0) - rate[s] * g["minutes"]) / math.sqrt(max(phi[s] * rate[s] * g["minutes"], 1e-6)) for s in STATS]
             for rate, played in samples for g in played]
    if len(resid) > 50:
        R = np.nan_to_num(np.corrcoef(np.asarray(resid).T), nan=0.0)
        np.fill_diagonal(R, 1.0)
        w, V = np.linalg.eigh(R)
        R = V @ np.diag(np.clip(w, 1e-4, None)) @ V.T
        d = np.sqrt(np.diag(R))
        R = R / np.outer(d, d)
    else:
        R = np.eye(len(STATS))
    pos_rate = {g: {s: (v[s] / v["min"] if v["min"] else 0.0) for s in STATS} for g, v in pos_tot.items()}
    return {"phi": phi, "corr": R, "pos_rate": pos_rate, "n_resid": len(resid), "min_ratio": minutes_ratio_samples(h)}


MINUTES_ROLE_EDGES = (24.0, 12.0)   # starter >= 24 > rotation >= 12 > bench (own mean minutes)


def minutes_role(mu: float) -> str:
    return "starter" if mu >= MINUTES_ROLE_EDGES[0] else ("rotation" if mu >= MINUTES_ROLE_EDGES[1] else "bench")


def minutes_ratio_samples(h: History, cap: int = 20000) -> dict:
    """Empirical distribution of (minutes played / player's own mean) by role, from games
    before the cutoff: captures early exits (injury, fouls, ejection) and blowout benching
    that a symmetric minutes distribution cannot."""
    out: dict[str, list] = {"starter": [], "rotation": [], "bench": []}
    for games in h.by_player.values():
        ms = [g["minutes"] for g in games if g["played"]]
        if len(ms) < 10:
            continue
        mu = sum(ms) / len(ms)
        out[minutes_role(mu)].extend(m / mu for m in ms)
    rng = np.random.default_rng(0)
    res = {}
    for k, v in out.items():
        arr = np.asarray(v, dtype=float)
        if len(arr) > cap:
            arr = rng.choice(arr, cap, replace=False)
        res[k] = np.sort(arr) if len(arr) >= 200 else None
    return res
