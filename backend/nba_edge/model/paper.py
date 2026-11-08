"""Paper-trading ledger: record, grade and report PAPER_CANDIDATE recommendations.

REAL lines, REAL outcomes, NO money. This is the only evidence track that can say anything
about betting performance; the simulated-line backtest (model/backtest.py) measures
projection calibration only and is never mixed into these numbers.

* Recording: the FIRST time a quote qualifies as PAPER_CANDIDATE under a model version, the
  exact line/price captured at that moment, the projection, probability, EV and gate results
  are frozen in `paper_recommendations` (insert-only, DB-trigger protected). Later runs that
  re-qualify the same quote do not add rows (no double counting, no cherry-picking a better
  price after the fact).
* Grading: once ESPN's final box score is in, the outcome is graded at the RECOMMENDATION
  price (1-unit notional stake; void if the player did not play, refunded push), and the
  closing price is taken from the append-only odds snapshots (last state at or before tip).
  CLV is recorded only when an odds capture ran after the recommendation and before tip.
* Reporting: Brier / log loss / calibration vs the market's no-vig baseline, ROI with a 95%
  interval, CLV, broken down by market, sportsbook, probability bucket, side, main/alternate.

Nothing here enables real-money betting or makes a profitability claim. The report states
the sample size and how far it is from the minimum the project set before any review.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta

from sqlalchemy import and_, func, insert, select
from sqlalchemy.engine import Connection, Engine

from ..db.paper_schema import paper_outcomes, paper_recommendations
from ..db.research_schema import nba_games, nba_player_games, prop_evaluations, prop_grades
from ..db.schema import odds_snapshots, snapshot_runs, utcnow
from .config import MARKET_STATS, MODEL_VERSION
from .metrics import summarize_binary

MIN_GRADED_FOR_REVIEW = 300          # decided paper results before ANY review of real-money use
REAL_MONEY_BETTING = "disabled"      # there is no code path that places bets; this is a label, not a switch
PROB_BUCKETS = [(0.0, 0.40), (0.40, 0.50), (0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 1.01)]

_HASH_FIELDS = ("rec_key", "recommended_at", "run_id", "model_version", "slate_date_et", "espn_event_id", "commence_time", "provider",
                "provider_event_id", "bookmaker", "quote_key", "athlete_id", "player_name", "team", "opponent", "market_key", "market_code",
                "is_alternate", "side", "line", "american_odds", "decimal_odds", "quote_seen_at", "odds_snapshot_id", "projection_mean",
                "projection_median", "projection_sd", "minutes_mean", "p_play", "model_prob", "model_push_prob", "model_prob_sd",
                "model_prob_low", "implied_prob", "novig_prob", "novig_method", "fair_american", "ev", "ev_low", "confidence", "gates",
                "stake_units")


def content_hash(rec: dict) -> str:
    canon = {k: (rec.get(k).isoformat() if isinstance(rec.get(k), datetime) else rec.get(k)) for k in _HASH_FIELDS}
    return hashlib.sha256(json.dumps(canon, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def verify_hashes(engine: Engine) -> dict:
    """Recompute every recommendation's hash (tamper evidence for exports/backups)."""
    bad = []
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(paper_recommendations))]
    for r in rows:
        if content_hash(r) != r["content_sha256"]:
            bad.append(r["id"])
    return {"checked": len(rows), "mismatched_ids": bad}


# ------------------------------------------------------------------ recording
def record_recommendations(conn: Connection, *, run_id: int, now: datetime, candidates: list[dict]) -> int:
    """Insert newly qualified PAPER_CANDIDATEs. `candidates` are prop_evaluations-shaped dicts
    plus slate_date_et, commence_time, team, opponent, projection_* and minutes_mean."""
    if not candidates:
        return 0
    keys = {f"{MODEL_VERSION}|{c['quote_key']}" for c in candidates}
    have = {r[0] for r in conn.execute(select(paper_recommendations.c.rec_key).where(paper_recommendations.c.rec_key.in_(keys)))}
    n = 0
    for c in candidates:
        key = f"{MODEL_VERSION}|{c['quote_key']}"
        if key in have:
            continue
        snap_id = conn.execute(select(odds_snapshots.c.id).where(and_(odds_snapshots.c.quote_key == c["quote_key"], odds_snapshots.c.captured_at <= now))
                               .order_by(odds_snapshots.c.captured_at.desc(), odds_snapshots.c.id.desc()).limit(1)).scalar()
        rec = dict(rec_key=key, recommended_at=now, run_id=run_id, model_version=MODEL_VERSION, slate_date_et=c["slate_date_et"],
                   espn_event_id=c["espn_event_id"], commence_time=c.get("commence_time"), provider=c["provider"],
                   provider_event_id=c["provider_event_id"], bookmaker=c["bookmaker"], quote_key=c["quote_key"], athlete_id=c["athlete_id"],
                   player_name=c["player_name"], team=c.get("team"), opponent=c.get("opponent"), market_key=c["market_key"],
                   market_code=c["market_code"], is_alternate=bool(c["is_alternate"]), side=c["side"], line=float(c["line"]),
                   american_odds=int(c["american_odds"]), decimal_odds=float(c["decimal_odds"]), quote_seen_at=c.get("quote_seen_at"),
                   odds_snapshot_id=snap_id, projection_mean=c.get("projection_mean"), projection_median=c.get("projection_median"),
                   projection_sd=c.get("projection_sd"), minutes_mean=c.get("minutes_mean"), p_play=float(c["p_play"]),
                   model_prob=float(c["model_prob"]), model_push_prob=float(c["model_push_prob"] or 0.0), model_prob_sd=c.get("model_prob_sd"),
                   model_prob_low=c.get("model_prob_low"), implied_prob=float(c["implied_prob"]), novig_prob=c.get("novig_prob"),
                   novig_method=c.get("novig_method"), fair_american=c.get("fair_american"), ev=float(c["ev"]), ev_low=c.get("ev_low"),
                   confidence=c.get("confidence"), gates=c.get("reasons"), stake_units=1.0)
        rec["content_sha256"] = content_hash(rec)
        conn.execute(insert(paper_recommendations).values(**rec))
        have.add(key)
        n += 1
    return n


# ------------------------------------------------------------------ grading
def _result(actual: float | None, line: float, side: str) -> str:
    if actual is None:
        return "void"
    if actual == line:
        return "push"
    return "win" if (actual > line) == (side == "over") else "loss"


def _profit(result: str, dec: float, stake: float) -> float:
    return round(stake * (dec - 1), 6) if result == "win" else (-stake if result == "loss" else 0.0)


def closing_state(conn: Connection, quote_key: str, tip: datetime) -> dict:
    """Last stored state of a quote at or before tip-off. Snapshots are change-only, so the
    state 'as of tip' is the latest row <= tip; if that row is a 'removed' tombstone the quote
    was pulled before tip and the last ACTIVE price is reported with closing_offered=False."""
    rows = conn.execute(select(odds_snapshots.c.american_odds, odds_snapshots.c.decimal_odds, odds_snapshots.c.market_status,
                               odds_snapshots.c.captured_at, odds_snapshots.c.provider)
                        .where(and_(odds_snapshots.c.quote_key == quote_key, odds_snapshots.c.captured_at <= tip))
                        .order_by(odds_snapshots.c.captured_at.desc(), odds_snapshots.c.id.desc()).limit(50)).all()
    if not rows:
        return {}
    offered = rows[0].market_status == "active"
    active = next((r for r in rows if r.market_status == "active" and r.decimal_odds), None)
    # when was this state last CONFIRMED: the latest successful capture before tip by the same provider
    confirmed = conn.execute(select(func.max(snapshot_runs.c.started_at)).where(and_(
        snapshot_runs.c.provider == rows[0].provider, snapshot_runs.c.status.in_(["ok", "partial"]), snapshot_runs.c.started_at <= tip))).scalar()
    return {"closing_american": active.american_odds if active else None, "closing_decimal": active.decimal_odds if active else None,
            "closing_captured_at": confirmed, "closing_offered": offered,
            "closing_minutes_before_tip": round((tip - confirmed).total_seconds() / 60, 1) if confirmed else None}


def grade_paper(engine: Engine, now: datetime | None = None) -> dict:
    now = now or utcnow()
    r, o = paper_recommendations.c, paper_outcomes.c
    with engine.connect() as conn:
        pending = [dict(x._mapping) for x in conn.execute(
            select(paper_recommendations, nba_games.c.start_time.label("game_start")).outerjoin(paper_outcomes, o.recommendation_id == r.id)
            .join(nba_games, nba_games.c.espn_event_id == r.espn_event_id)
            .where(and_(o.recommendation_id.is_(None), nba_games.c.completed.is_(True), nba_games.c.box_ingested.is_(True))))]
        if not pending:
            return {"graded": 0}
        box = {(b.espn_event_id, b.athlete_id): dict(b._mapping) for b in conn.execute(
            select(nba_player_games).where(nba_player_games.c.espn_event_id.in_({p["espn_event_id"] for p in pending})))}
        rows = []
        for p in pending:
            b = box.get((p["espn_event_id"], p["athlete_id"]))
            actual = float(sum(b[s] or 0 for s in MARKET_STATS[p["market_code"]])) if b and b["played"] else None
            res = _result(actual, p["line"], p["side"])
            tip = p["commence_time"] or p["game_start"]
            cl = closing_state(conn, p["quote_key"], tip) if tip else {}
            # CLV only if the quote was re-observed after the recommendation (else it would read 0)
            observed_later = bool(cl.get("closing_captured_at")) and cl["closing_captured_at"] > p["recommended_at"]
            clv = (1 / cl["closing_decimal"] - 1 / p["decimal_odds"]) if cl.get("closing_decimal") and observed_later else None
            rows.append(dict(recommendation_id=p["id"], graded_at=now, actual=actual, result=res,
                             profit_units=_profit(res, p["decimal_odds"], p["stake_units"]), clv_prob=round(clv, 6) if clv is not None else None,
                             outcome_source="espn_box_score", **{k: cl.get(k) for k in ("closing_american", "closing_decimal", "closing_captured_at",
                                                                                         "closing_minutes_before_tip", "closing_offered")}))
    with engine.begin() as conn:
        conn.execute(insert(paper_outcomes), rows)
    return {"graded": len(rows), "by_result": {k: sum(1 for x in rows if x["result"] == k) for k in ("win", "loss", "push", "void")}}


# ------------------------------------------------------------------ reporting
def _bucket(p: float) -> str:
    for lo, hi in PROB_BUCKETS:
        if lo <= p < hi:
            return f"{lo:.2f}-{min(hi, 1.0):.2f}"
    return "n/a"


def _p_decided(r: dict) -> float:
    return r["model_prob"] / max(1e-9, 1 - (r.get("model_push_prob") or 0.0))


def _group_stats(rows: list[dict], with_roi: bool) -> dict:
    dec = [x for x in rows if x["result"] in ("win", "loss")]
    out = {"n": len(rows), "wins": sum(x["result"] == "win" for x in rows), "losses": sum(x["result"] == "loss" for x in rows),
           "pushes": sum(x["result"] == "push" for x in rows), "voids": sum(x["result"] == "void" for x in rows)}
    if dec:
        ys = [int(x["result"] == "win") for x in dec]
        ps = [_p_decided(x) for x in dec]
        out["observed_win_rate"] = round(sum(ys) / len(ys), 4)
        out["mean_model_prob"] = round(sum(ps) / len(ps), 4)
        out["brier_model"] = round(sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ys), 5)
        nv = [(x["novig_prob"], y) for x, y in zip(dec, ys) if x.get("novig_prob") is not None]
        out["brier_market_novig"] = round(sum((p - y) ** 2 for p, y in nv) / len(nv), 5) if nv else None
        out["n_with_novig"] = len(nv)
    clv = [x["clv_prob"] for x in rows if x.get("clv_prob") is not None and x["result"] != "void"]
    out["clv"] = {"n": len(clv), "mean_prob": round(sum(clv) / len(clv), 5) if clv else None,
                  "pct_beat_close": round(sum(c > 0 for c in clv) / len(clv), 4) if clv else None,
                  "pct_equal_close": round(sum(c == 0 for c in clv) / len(clv), 4) if clv else None}
    if with_roi:
        staked = [x for x in rows if x["result"] != "void"]      # voids are refunded: no stake at risk
        profits = [x["profit_units"] for x in staked]
        n = len(profits)
        units = sum(profits)
        out["staked_units"] = float(sum(x.get("stake_units", 1.0) for x in staked))
        out["profit_units"] = round(units, 3)
        if n:
            mean = units / out["staked_units"]
            sd = math.sqrt(sum((p - units / n) ** 2 for p in profits) / (n - 1)) if n > 1 else None
            out["roi"] = round(mean, 4)
            out["roi_ci95"] = [round(mean - 1.96 * sd / math.sqrt(n), 4), round(mean + 1.96 * sd / math.sqrt(n), 4)] if sd else None
            out["mean_ev_at_recommendation"] = round(sum(x["ev"] for x in staked) / n, 4) if all(x.get("ev") is not None for x in staked) else None
    return out


BREAKDOWNS = {
    "market": lambda x: x["market_code"],
    "sportsbook": lambda x: x["bookmaker"],
    "probability_bucket": lambda x: _bucket(_p_decided(x)),
    "side": lambda x: x["side"],
    "line_type": lambda x: "alternate" if x["is_alternate"] else "main",
}


def _breakdowns(rows: list[dict], with_roi: bool, extra: dict | None = None) -> dict:
    out = {}
    for name, fn in {**BREAKDOWNS, **(extra or {})}.items():
        groups: dict[str, list] = {}
        for x in rows:
            groups.setdefault(str(fn(x)), []).append(x)
        out[name] = {k: _group_stats(v, with_roi) for k, v in sorted(groups.items())}
    return out


def paper_report(engine: Engine, now: datetime | None = None) -> dict:
    """The REAL paper-trading track. Never mixed with simulated backtests."""
    now = now or utcnow()
    with engine.connect() as conn:
        recs = [dict(x._mapping) for x in conn.execute(select(paper_recommendations, paper_outcomes.c.result, paper_outcomes.c.actual,
                                                              paper_outcomes.c.profit_units, paper_outcomes.c.closing_american,
                                                              paper_outcomes.c.closing_offered, paper_outcomes.c.clv_prob, paper_outcomes.c.graded_at)
                                                       .outerjoin(paper_outcomes, paper_outcomes.c.recommendation_id == paper_recommendations.c.id)
                                                       .order_by(paper_recommendations.c.id))]
    graded = [r for r in recs if r["result"] is not None]
    pending = [r for r in recs if r["result"] is None]
    decided = [r for r in graded if r["result"] in ("win", "loss")]
    out = {
        "track": "PAPER — real sportsbook lines and prices captured at recommendation time, graded against final box scores. "
                 "Notional 1-unit stakes. No real bets were placed.",
        "real_money_betting": REAL_MONEY_BETTING,
        "profitability_claim": "none",
        "as_of": now.isoformat(),
        "n_recommendations": len(recs), "n_graded": len(graded), "n_pending": len(pending),
        "n_pending_older_than_2_days": sum(1 for r in pending if r["commence_time"] and r["commence_time"] < now - timedelta(days=2)),
        "n_independent_positions": len({(r["espn_event_id"], r["athlete_id"], r["market_code"], r["side"]) for r in recs}),
        "evidence_status": {"decided_results": len(decided), "minimum_before_any_review": MIN_GRADED_FOR_REVIEW,
                            "status": "insufficient_sample" if len(decided) < MIN_GRADED_FOR_REVIEW else "minimum_sample_reached_review_required",
                            "note": "Reaching the minimum sample only allows a review of the evidence; it does not establish profitability."},
        "model_versions": sorted({r["model_version"] for r in recs}),
    }
    if graded:
        out["overall"] = _group_stats(graded, with_roi=True)
        if decided:
            out["calibration"] = summarize_binary([_p_decided(r) for r in decided], [int(r["result"] == "win") for r in decided])
        out["by"] = _breakdowns(graded, with_roi=True, extra={"model_version": lambda x: x["model_version"]})
    out["recent"] = [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items() if k not in ("gates", "content_sha256", "rec_key")}
                     for r in recs[-50:][::-1]]
    return out


def calibration_track(engine: Engine) -> dict:
    """All evaluated quotes on REAL lines (each quote's last pre-tip evaluation), graded.
    Calibration and CLV only — no ROI: this is every quote, not a betting strategy."""
    e, g = prop_evaluations.c, prop_grades.c
    with engine.connect() as conn:
        last = select(e.quote_key, func.max(e.id).label("mid")).join(prop_grades, g.evaluation_id == e.id).group_by(e.quote_key).subquery()
        rows = [dict(r._mapping) for r in conn.execute(select(prop_evaluations, prop_grades.c.result, prop_grades.c.clv_prob)
                                                       .join(prop_grades, g.evaluation_id == e.id).join(last, last.c.mid == e.id))]
    rows = [r for r in rows if r["model_prob"] is not None]
    dec = [r for r in rows if r["result"] in ("win", "loss")]
    out = {"track": "ALL QUOTES — real lines + real outcomes, calibration only (not a betting strategy, no ROI)",
           "n_graded_quotes": len(rows), "n_decided": len(dec)}
    if dec:
        ys = [int(r["result"] == "win") for r in dec]
        out["model"] = summarize_binary([_p_decided(r) for r in dec], ys)
        nv = [(r["novig_prob"], y) for r, y in zip(dec, ys) if r["novig_prob"] is not None]
        out["market_novig_baseline"] = summarize_binary([a for a, _ in nv], [b for _, b in nv]) if nv else None
        out["by"] = _breakdowns(rows, with_roi=False, extra={"decision": lambda x: x["decision"]})
    return out
