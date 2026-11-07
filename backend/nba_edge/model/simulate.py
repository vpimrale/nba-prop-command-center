"""Monte Carlo: posterior-predictive joint distribution of a player's stats.

Per simulation:  minutes ~ TruncNormal(mu, sd, [1, 48])
                 rate_s  ~ Gamma posterior (one draw per batch -> parameter uncertainty)
                 mean_s  = rate_s * minutes * context_factor_s
                 stat_s  = NegBinomial(mean_s, Var = phi_s * mean_s)  [Poisson if phi≈1]
Stats within a player are joined with a Gaussian copula whose correlation matrix is
estimated from league residuals, so combos (PRA, PR, PA, RA) are sums of jointly drawn
components — never products of independent probabilities. All draws are CONDITIONAL on
the player playing; availability (p_play) is handled at pricing time (props are void if
the player does not play).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import special, stats as st

from .config import COMBOS, STAT_CODE, STATS, ModelConfig
from .history import minutes_role
from .project import PlayerParams


@dataclass
class SimResult:
    athlete_id: str
    minutes: np.ndarray
    stats: dict            # market code -> int array (n_sims,)
    batch: np.ndarray      # batch index per sim
    n_batches: int


def _trunc_normal(rng, mu, sd, lo, hi, n):
    x = rng.normal(mu, sd, n)
    for _ in range(5):
        bad = (x < lo) | (x > hi)
        if not bad.any():
            break
        x[bad] = rng.normal(mu, sd, bad.sum())
    return np.clip(x, lo, hi)


def simulate_player(p: PlayerParams, league: dict, cfg: ModelConfig, rng: np.random.Generator, n_sims: int | None = None) -> SimResult:
    n = n_sims or cfg.n_sims
    B = cfg.n_batches
    m = n // B
    n = m * B
    batch = np.repeat(np.arange(B), m)
    phi = league["phi"]
    ratios = (league.get("min_ratio") or {}).get(minutes_role(p.min_mu)) if cfg.minutes_model == "empirical_ratio" else None
    if ratios is not None:
        minutes = np.clip(p.min_mu * rng.choice(ratios, n), 1.0, 48.0)
        adj = p.notes.get("team", {}).get("minutes_adjust_sd", 0.0) if isinstance(p.notes.get("team"), dict) else 0.0
        if adj:
            minutes = np.clip(minutes + rng.normal(0, adj, n), 1.0, 48.0)
    else:
        minutes = _trunc_normal(rng, p.min_mu, max(p.min_sd, 1.0), 1.0, 48.0, n)
    form = np.exp(rng.normal(-0.5 * cfg.form_sigma ** 2, cfg.form_sigma, n)) if cfg.form_sigma > 0 else 1.0
    L = np.linalg.cholesky(league["corr"])
    z = rng.standard_normal((n, len(STATS))) @ L.T
    u = np.clip(special.ndtr(z), 1e-12, 1 - 1e-12)
    out: dict[str, np.ndarray] = {}
    for j, s in enumerate(STATS):
        ph = max(1.0, phi.get(s, 1.0))
        a, b = p.rate_a[s], p.rate_b[s]
        rates = rng.gamma(max(a / ph, 1e-6), ph / b, B) if b > 0 else np.zeros(B)
        mean = rates[batch] * minutes * p.factor.get(s, 1.0) * form
        mean = np.maximum(mean, 1e-9)
        if ph <= 1.02:
            y = st.poisson.ppf(u[:, j], mean)
        else:
            r = mean / (ph - 1.0)
            y = st.nbinom.ppf(u[:, j], r, 1.0 / ph)
        out[STAT_CODE[s]] = np.nan_to_num(y, nan=0.0).astype(np.int32)
    for code, comps in COMBOS.items():
        out[code] = sum(out[STAT_CODE[c]] for c in comps)
    return SimResult(p.athlete_id, minutes, out, batch, B)


def summarize(sim: SimResult) -> dict:
    res = {"MIN": _summ(sim.minutes)}
    for code, arr in sim.stats.items():
        res[code] = _summ(arr)
    return res


def _summ(x: np.ndarray) -> dict:
    q = np.quantile(x, [0.1, 0.25, 0.5, 0.75, 0.9])
    return {"mean": float(x.mean()), "sd": float(x.std()), "p10": float(q[0]), "p25": float(q[1]), "median": float(q[2]),
            "p75": float(q[3]), "p90": float(q[4])}


def line_probs(sim: SimResult, code: str, line: float, side: str) -> dict:
    """P(win), P(push), P(loss) for a bet on `side` of `line`, conditional on playing, plus
    the between-batch spread of P(win) net of Monte Carlo noise (parameter uncertainty)."""
    x = sim.stats[code]
    if side == "over":
        win, push = x > line, x == line
    elif side == "under":
        win, push = x < line, x == line
    else:
        raise ValueError(side)
    pw, pp = float(win.mean()), float(push.mean())
    per_batch = np.bincount(sim.batch, weights=win.astype(float), minlength=sim.n_batches) / np.bincount(sim.batch, minlength=sim.n_batches)
    m = len(x) / sim.n_batches
    mc_var = pw * (1 - pw) / m
    unc = float(np.sqrt(max(per_batch.var(ddof=1) - mc_var, 0.0)))
    return {"p_win": pw, "p_push": pp, "p_loss": 1 - pw - pp, "p_unc": unc, "mc_se": float(np.sqrt(pw * (1 - pw) / len(x)))}
