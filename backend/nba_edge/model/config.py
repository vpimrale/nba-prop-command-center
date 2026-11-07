"""Model constants. Every number here is either estimated from data at run time (see
context.py) or an explicit, documented prior/heuristic. Heuristics are marked; they are
the first things the backtest/calibration reports should be used to revisit."""
from __future__ import annotations

from dataclasses import dataclass, field

MODEL_VERSION = "v1.2-shrunk-nb-copula-empmin"

STATS = ("pts", "reb", "ast", "fg3m", "stl", "blk", "tov")
STAT_CODE = {"pts": "PTS", "reb": "REB", "ast": "AST", "fg3m": "3PM", "stl": "STL", "blk": "BLK", "tov": "TO"}
COMBOS = {"PRA": ("pts", "reb", "ast"), "PR": ("pts", "reb"), "PA": ("pts", "ast"), "RA": ("reb", "ast")}
MARKET_STATS = {**{c: (s,) for s, c in STAT_CODE.items()}, **COMBOS}   # market code -> component stats
USAGE_STATS = ("pts", "ast", "fg3m", "tov")   # boosted when high-usage teammates are out

# Availability from injury status (probability the player records >0 minutes).
# Heuristic mapping, documented; ESPN's own statuses are coarse (mostly Out / Day-To-Day).
P_PLAY_BY_STATUS = {"out": 0.0, "doubtful": 0.15, "questionable": 0.5, "day-to-day": 0.65, "probable": 0.9,
                    "available": 0.98, None: 0.98}


@dataclass(frozen=True)
class ModelConfig:
    n_sims: int = 10_000
    n_batches: int = 20                 # parameter draws; between-batch spread = estimation uncertainty
    # window weights: a current-season game counts once for "season", again if in L20, again if in L10
    window_weights: tuple[int, int, int] = (5, 3, 1)     # chosen by walk-forward backtest (v1.2), see docs/MODEL.md
    # Prior strength, in pseudo-MINUTES at the player's prior rate (noisier stats shrink more).
    k_prior_minutes: dict = field(default_factory=lambda: {"pts": 120, "reb": 150, "ast": 150, "fg3m": 225, "stl": 350, "blk": 350, "tov": 250})
    k_prev_to_league_minutes: float = 400.0   # previous-season rate shrunk to league positional rate
    min_prev_minutes_for_prior: float = 150.0
    k_minutes_games: float = 4.0              # minutes mean shrink toward prior role (games)
    minutes_sd_prior: float = 5.5
    k_minutes_sd_games: float = 6.0
    pace_k_games: float = 10.0
    opp_k_games: float = 12.0
    opp_exponent: float = 0.5                 # heuristic: half-strength opponent effect (partial pooling)
    usage_boost_coef: float = 0.35            # heuristic: share of lost team usage re-absorbed by teammates
    usage_boost_cap: float = 1.08
    # Minutes of known-absent rotation players are redistributed to available teammates
    # (scale >= 1), capped: a bigger implied jump is usually missing information, not minutes.
    minutes_scale_band: tuple[float, float] = (1.0, 1.10)
    minutes_cap: float = 42.0
    b2b_minutes_factor: float = 0.97          # heuristic for heavy-minute players on the 2nd night
    blowout_margin_threshold: float = 9.0     # heuristic: expected margin (pts) where starters' minutes start to fall
    blowout_minutes_per_point: float = 0.25
    min_games_for_projection: int = 3         # played games across both seasons
    # Per-player-game "form" shock shared by all of that player's stats in a simulation
    # (shooting nights, role swings): mean-1 lognormal with this sigma. Chosen from the
    # walk-forward backtest's PIT/calibration (see docs/MODEL.md), not assumed.
    form_sigma: float = 0.2
    # "empirical_ratio": minutes = projected mean x a draw from the league's empirical
    # minutes/own-mean distribution for the player's role (data-driven tails);
    # "normal": truncated normal (v1.0/v1.1 behaviour).
    minutes_model: str = "empirical_ratio"
    dnp_k_games: float = 5.0


@dataclass(frozen=True)
class DecisionConfig:
    target_books: tuple[str, ...] = ("fanduel", "fanatics")
    min_ev_main: float = 0.03                 # EV at the conservative probability bound, per unit
    min_ev_alt: float = 0.06
    calibration_margin_main: float = 0.02    # extra probability haircut until the model is calibrated live
    calibration_margin_alt: float = 0.04
    max_model_market_gap: float = 0.12       # larger gaps are treated as probable model error
    min_p_play: float = 0.90
    min_games_sample: int = 10
    min_games_this_season: int = 3
    max_quote_age_min: float = 20.0
    min_decimal: float = 1.25
    max_decimal: float = 5.0
    max_decimal_alt: float = 3.0              # no longshot alternates: their no-vig price is only approximate
    max_main_line_gap_for_alt: float = 0.05   # alt candidates need the model to agree with the market's main line
    calibration_min_graded: int = 300        # below this many live graded props, label PAPER only
