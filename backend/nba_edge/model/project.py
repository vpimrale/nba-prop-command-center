"""Per-player projection parameters for one game: availability, minutes distribution,
posterior per-minute rates (season / L20 / L10 blend, shrunk to a prior), and context
multipliers (pace, opponent, home/away, teammate usage, rest, blowout).

Every adjustment is recorded in `notes` so the research view can show exactly why a
projection is what it is.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from .config import MODEL_VERSION, P_PLAY_BY_STATUS, STATS, USAGE_STATS, ModelConfig
from .history import History, pos_group


@dataclass
class GameCtx:
    espn_event_id: str
    home: str
    away: str
    start_time: datetime
    date_et: str


@dataclass
class PlayerParams:
    athlete_id: str
    player_name: str
    team: str
    opponent: str
    is_home: bool
    position: str | None
    p_play: float
    min_mu: float
    min_sd: float
    rate_a: dict             # Gamma posterior shape per stat (per-minute rate), before phi inflation
    rate_b: dict             # Gamma posterior rate (pseudo-minutes)
    factor: dict             # context multiplier per stat
    games_this_season: int
    games_sample: int
    injury_status: str | None
    research: dict = field(default_factory=dict)
    notes: dict = field(default_factory=dict)

    def rate_mean(self, s: str) -> float:
        return self.rate_a[s] / self.rate_b[s] if self.rate_b[s] else 0.0


def _weights(n: int, cfg: ModelConfig) -> list[int]:
    w10, w20, wseason = cfg.window_weights
    return [w10 if i < 10 else (w20 if i < 20 else wseason) for i in range(n)]


def _window(games: list[dict], k: int | None, stat: str) -> dict:
    g = games[:k] if k else games
    vals = [x[stat] if stat != "minutes" else x["minutes"] for x in g]
    return {"n": len(vals), "mean": (float(np.mean(vals)) if vals else None), "values": vals[:20] if k else None}


def _status_key(effective: str | None) -> str | None:
    if not effective:
        return None
    e = effective.lower()
    for k in ("out", "doubtful", "questionable", "day-to-day", "probable", "available"):
        if k in e:
            return k
    return "day-to-day"


def player_params(h: History, aid: str, team: str, game: GameCtx, injury: dict | None, cfg: ModelConfig) -> PlayerParams | None:
    games = h.by_player.get(aid, [])
    played = [g for g in games if g["played"]]
    if len(played) < cfg.min_games_for_projection:
        return None
    cur = [g for g in played if g["season"] == h.season]
    prev = [g for g in played if g["season"] == h.season - 1]
    name = games[0]["player_name"]
    position = next((g["position"] for g in games if g["position"]), None)
    grp = pos_group(position)
    pos_rate = h.league["pos_rate"].get(grp) or {s: 0.0 for s in STATS}
    notes: dict = {"model_version": MODEL_VERSION}

    # ---- availability
    status = (injury or {}).get("effective")
    skey = _status_key(status)
    p_status = P_PLAY_BY_STATUS.get(skey, P_PLAY_BY_STATUS[None])
    recent_rows = [g for g in games if g["season"] == h.season][:10]
    dnp = sum(1 for g in recent_rows if not g["played"])
    dnp_rate = (dnp + 0.05 * cfg.dnp_k_games) / (len(recent_rows) + cfg.dnp_k_games)
    team_dates = sorted({t["date_et"] for t in h.team_games if t["team"] == team and t["season"] == h.season}, reverse=True)
    last_played = played[0]["date_et"]
    missed_since = sum(1 for d in team_dates if d > last_played)
    recency = 1.0 if missed_since < 3 else 0.2   # absent from the team's last 3+ box scores
    p_play = p_status * (1 - dnp_rate) * recency
    notes["availability"] = {"injury_status": status, "p_status": p_status, "dnp_rate": round(dnp_rate, 3),
                             "team_games_missed_since_last_played": missed_since, "p_play": round(p_play, 3)}

    # ---- minutes
    w = _weights(len(cur), cfg)
    if cur:
        sw = sum(w)
        mu_cur = sum(wi * g["minutes"] for wi, g in zip(w, cur)) / sw
        n_eff = sw * sw / sum(wi * wi for wi in w)
        var_cur = sum(wi * (g["minutes"] - mu_cur) ** 2 for wi, g in zip(w, cur)) / sw if len(cur) >= 3 else cfg.minutes_sd_prior ** 2
    else:
        mu_cur, n_eff, var_cur = 0.0, 0.0, cfg.minutes_sd_prior ** 2
    prior_min = float(np.mean([g["minutes"] for g in prev])) if len(prev) >= 10 else (mu_cur if cur else float(np.mean([g["minutes"] for g in played])))
    k = cfg.k_minutes_games
    min_mu = (n_eff * mu_cur + k * prior_min) / (n_eff + k) if cur else prior_min
    min_sd = math.sqrt((n_eff * var_cur + cfg.k_minutes_sd_games * cfg.minutes_sd_prior ** 2) / (n_eff + cfg.k_minutes_sd_games))
    notes["minutes"] = {"cur_weighted_mean": round(mu_cur, 2), "cur_games": len(cur), "prior_mean": round(prior_min, 2),
                        "n_eff": round(n_eff, 2), "shrunk_mean": round(min_mu, 2), "sd": round(min_sd, 2)}

    # ---- rates (per minute): weighted current season, prior = previous season shrunk to positional league rate
    rate_a, rate_b = {}, {}
    sw = sum(w) if cur else 0
    scale = (n_eff / sw) if sw else 0.0
    prev_min = sum(g["minutes"] for g in prev)
    for s in STATS:
        prev_y = sum(g[s] or 0 for g in prev)
        k0 = cfg.k_prev_to_league_minutes
        prior = (prev_y + k0 * pos_rate[s]) / (prev_min + k0) if prev_min >= cfg.min_prev_minutes_for_prior else pos_rate[s]
        wy = sum(wi * (g[s] or 0) for wi, g in zip(w, cur)) * scale
        wm = sum(wi * g["minutes"] for wi, g in zip(w, cur)) * scale
        K = cfg.k_prior_minutes[s] if prev_min >= cfg.min_prev_minutes_for_prior else cfg.k_prior_minutes[s] * 0.5
        if not cur:  # no current-season games: lean on last season directly
            wy, wm, K = prev_y, prev_min, cfg.k_prior_minutes[s]
            prior = pos_rate[s]
        rate_a[s] = K * prior + wy
        rate_b[s] = K + wm
    notes["rates_per36"] = {s: round(36 * rate_a[s] / rate_b[s], 2) for s in STATS}

    # ---- context multipliers
    lg = h.league
    opp = game.away if team == game.home else game.home
    is_home = team == game.home
    pace_opp = lg["team_pace"].get(opp, lg["pace"])
    pace_f = pace_opp / lg["pace"] if lg["pace"] else 1.0
    opp_f = lg["opp_factor"].get(opp, {})
    home_f = math.sqrt(lg.get("home_factor") or 1.0)
    factor = {}
    for s in STATS:
        f = pace_f * (opp_f.get(s, 1.0) ** cfg.opp_exponent)
        if s in ("pts", "ast", "fg3m"):
            f *= home_f if is_home else 1 / home_f
        factor[s] = f
    notes["context"] = {"opponent": opp, "pace_factor": round(pace_f, 4), "opp_factor": {s: round(opp_f.get(s, 1.0), 4) for s in STATS},
                        "home_factor_applied": round(home_f if is_home else 1 / home_f, 4)}

    research = {"windows": {s: {"season": _window(cur, None, s), "L10": _window(cur, 10, s), "L20": _window(cur, 20, s),
                                "prev_season": _window(prev, None, s)} for s in (*STATS, "minutes")},
                "starter_rate_L10": (sum(1 for g in cur[:10] if g["starter"]) / min(10, len(cur))) if cur else None,
                "usage_per_min": _usage_rate(cur or prev)}
    return PlayerParams(aid, name, team, opp, is_home, position, p_play, min_mu, min_sd, rate_a, rate_b, factor, len(cur),
                        len(cur) + len(prev), status, research, notes)


def _usage_rate(games: list[dict]) -> float:
    m = sum(g["minutes"] for g in games)
    if not m:
        return 0.0
    return sum((g["fga"] or 0) + 0.44 * (g["fta"] or 0) + (g["tov"] or 0) for g in games) / m


def team_roster(h: History, team: str, cutoff: datetime, max_age_days: int = 45) -> list[str]:
    """Players whose most recent box-score team is `team` (handles trades), seen recently."""
    out = []
    since = (cutoff - timedelta(days=max_age_days)).date().isoformat()
    for aid, games in h.by_player.items():
        if games and games[0]["team"] == team and games[0]["date_et"] >= since:
            out.append(aid)
    return out


def team_adjust(players: list[PlayerParams], h: History, game: GameCtx, cfg: ModelConfig) -> None:
    """Team-level coherence: rest, blowout risk, minutes normalized to 240 (which redistributes
    minutes of absent rotation players) and a usage boost when high-usage teammates are out."""
    if not players:
        return
    team = players[0].team
    lg = h.league
    # rest: back-to-back
    team_dates = {t["date_et"] for t in h.team_games if t["team"] == team}
    prev_day = (datetime.fromisoformat(game.date_et) - timedelta(days=1)).date().isoformat()
    b2b = prev_day in team_dates
    # blowout: expected margin from shrunk net ratings
    opp = players[0].opponent
    pace = (lg["team_pace"].get(team, lg["pace"]) * lg["team_pace"].get(opp, lg["pace"]) / lg["pace"]) if lg["pace"] else 100
    margin = (lg["net"].get(team, 0.0) - lg["net"].get(opp, 0.0)) / 2 * pace / 100 + (1.5 if team == game.home else -1.5)
    # usage lost from recently-active teammates who are (likely) out
    lost_usage = 0.0
    total_usage = 0.0
    for p in players:
        u = p.research.get("usage_per_min", 0.0) * p.min_mu
        total_usage += u
        recently_active = p.notes["availability"]["team_games_missed_since_last_played"] < 3 and p.games_this_season >= 3
        if p.p_play <= 0.25 and recently_active:
            lost_usage += u * (1 - p.p_play)
    share = lost_usage / total_usage if total_usage else 0.0
    boost = min(cfg.usage_boost_cap, 1 + cfg.usage_boost_coef * share / max(1e-6, 1 - share))
    for p in players:
        if b2b and p.min_mu >= 28:
            p.min_mu *= cfg.b2b_minutes_factor
        if abs(margin) > cfg.blowout_margin_threshold and p.min_mu >= 28:
            cut = cfg.blowout_minutes_per_point * (abs(margin) - cfg.blowout_margin_threshold)
            p.min_mu -= cut
            p.min_sd += cut * 0.5
    # Explicit redistribution (v1.2): only the expected minutes of KNOWN-absent, recently
    # active players (injury status) are handed to available rotation teammates, in proportion
    # to their minutes. v1.1's blanket "normalize team minutes to 240" biased everyone low
    # whenever absences were unknown (walk-forward backtest: PTS bias -0.43 -> -0.03 without it).
    lost_min = 0.0
    for p in players:
        recently_active = p.notes["availability"]["team_games_missed_since_last_played"] < 3 and p.games_this_season >= 3
        if recently_active and p.p_play < 0.5 and p.injury_status:
            lost_min += p.min_mu * (1 - p.p_play)
    receivers = [p for p in players if p.p_play >= 0.5 and p.min_mu >= 10]
    pool = sum(p.min_mu for p in receivers)
    raw_scale = 1 + lost_min / pool if pool else 1.0
    expected = sum(p.p_play * p.min_mu for p in players if p.p_play > 0)
    lo, hi = cfg.minutes_scale_band
    scale = min(hi, max(lo, raw_scale))
    adjust_sd: dict[str, float] = {}
    for p in players:
        if p in receivers:
            before = p.min_mu
            p.min_mu = min(cfg.minutes_cap, p.min_mu * scale)
            adjust_sd[p.athlete_id] = abs(p.min_mu - before)   # adjustment uncertainty
            p.min_sd = (p.min_sd ** 2 + adjust_sd[p.athlete_id] ** 2) ** 0.5
        if share > 0 and p.p_play > 0.25:
            for s in USAGE_STATS:
                p.factor[s] *= boost
        p.notes["team"] = {"back_to_back": b2b, "expected_margin": round(margin, 2), "minutes_scale_to_240": round(scale, 4), "minutes_scale_raw": round(raw_scale, 4),
                           "team_expected_minutes_before_scale": round(expected, 1), "lost_usage_share": round(share, 4),
                           "usage_boost": round(boost if share > 0 else 1.0, 4), "final_minutes_mean": round(p.min_mu, 2),
                           "minutes_adjust_sd": round(adjust_sd.get(p.athlete_id, 0.0), 3)}
