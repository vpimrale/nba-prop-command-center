"""Pricing + decision layer. NO_BET is the default; a prop only becomes a PAPER_CANDIDATE
when every gate passes using a CONSERVATIVE probability (point estimate minus parameter
uncertainty minus a calibration margin). Raw hit rates are never used as probabilities.

EV per 1 unit staked, for a prop that is void if the player does not play:
    EV = p_play * [ P(win|play) * (decimal - 1) - P(loss|play) ]      (push and void return 0)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..odds.math import decimal_to_american
from .config import DecisionConfig


def implied(decimal: float) -> float:
    return 1.0 / decimal


def ev_per_unit(p_play: float, p_win: float, p_loss: float, decimal: float) -> float:
    return p_play * (p_win * (decimal - 1.0) - p_loss)


def fair_american(p_win: float, p_loss: float) -> int | None:
    denom = p_win + p_loss
    if p_win <= 0 or denom <= 0:
        return None
    q = p_win / denom          # win probability among decided outcomes (pushes refunded)
    if q >= 0.9999:
        return -1_000_000
    return decimal_to_american(1.0 / q)


def novig_two_way(dec_side: float, dec_other: float) -> float:
    a, b = 1 / dec_side, 1 / dec_other
    return a / (a + b)


@dataclass
class Evaluation:
    model_prob: float
    push_prob: float
    prob_sd: float
    prob_low: float
    p_play: float
    ev: float
    ev_low: float
    fair_american: int | None
    implied_prob: float
    novig_prob: float | None
    novig_method: str
    edge_vs_novig: float | None
    confidence: str
    decision: str
    reasons: list = field(default_factory=list)


def evaluate(*, p_win: float, p_push: float, p_unc: float, p_play: float, decimal: float, novig: float | None, novig_method: str,
             is_alternate: bool, bookmaker: str, stale: bool, quote_age_min: float | None, games_sample: int, games_this_season: int,
             live_graded: int, main_line_gap: float | None = None, cfg: DecisionConfig = DecisionConfig()) -> Evaluation:
    p_loss = max(0.0, 1 - p_win - p_push)
    ev = ev_per_unit(p_play, p_win, p_loss, decimal)
    margin = cfg.calibration_margin_alt if is_alternate else cfg.calibration_margin_main
    p_low = max(0.0, p_win - p_unc - margin)
    ev_low = ev_per_unit(p_play, p_low, max(0.0, 1 - p_low - p_push), decimal)
    edge = (p_win / max(1e-9, p_win + p_loss) - novig) if novig is not None else None
    if p_unc < 0.04 and games_sample >= 20 and p_play >= 0.95 and novig_method == "two_way":
        conf = "high"
    elif p_unc < 0.07 and games_sample >= cfg.min_games_sample:
        conf = "medium"
    else:
        conf = "low"
    gates = [
        ("target_book", bookmaker in cfg.target_books),
        ("fresh_quote", (not stale) and quote_age_min is not None and quote_age_min <= cfg.max_quote_age_min),
        ("availability", p_play >= cfg.min_p_play),
        ("sample_size", games_sample >= cfg.min_games_sample and games_this_season >= cfg.min_games_this_season),
        ("confidence", conf != "low"),
        ("price_range", cfg.min_decimal <= decimal <= (cfg.max_decimal_alt if is_alternate else cfg.max_decimal)),
        ("market_agreement", edge is not None and abs(edge) <= cfg.max_model_market_gap),
        ("main_line_consistency", (not is_alternate) or (main_line_gap is not None and abs(main_line_gap) <= cfg.max_main_line_gap_for_alt)),
        ("ev_conservative", ev_low >= (cfg.min_ev_alt if is_alternate else cfg.min_ev_main)),
    ]
    reasons = [{"gate": g, "pass": bool(ok)} for g, ok in gates]
    if all(ok for _, ok in gates):
        decision = "PAPER_CANDIDATE"   # never "BET": the model is not yet validated on live outcomes
        if live_graded < cfg.calibration_min_graded:
            reasons.append({"note": f"paper-trade only: {live_graded} live graded props < {cfg.calibration_min_graded} needed before trusting calibration"})
    elif ev > 0 and edge is not None and 0 < edge <= cfg.max_model_market_gap and dict(gates)["price_range"]:
        decision = "WATCH"      # positive but fails a gate; a big model/market gap is NO_BET (likely model error)
    else:
        decision = "NO_BET"
    return Evaluation(p_win, p_push, p_unc, p_low, p_play, ev, ev_low, fair_american(p_win, p_loss), implied(decimal), novig, novig_method,
                      edge, conf, decision, reasons)
