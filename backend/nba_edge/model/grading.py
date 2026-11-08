"""Grade stored evaluations against final ESPN box scores (REAL lines, REAL outcomes).

A graded evaluation is a record of what the model said about a real price before the game,
and what happened. This is the only source of genuine model-performance evidence; it is
reported separately from the simulated-line backtest. Paper only: no bets were placed.

CLV: implied probability of the last pre-tip snapshot of the same quote minus implied
probability at evaluation time (positive = the market moved toward the evaluated side).
Only computed when a successful odds capture ran after the evaluation and before tip-off;
otherwise it is unavailable (None), never a misleading 0.
"""
from __future__ import annotations

from sqlalchemy import and_, func, insert, select
from sqlalchemy.engine import Engine

from ..db.research_schema import nba_games, nba_player_games, prop_evaluations, prop_grades
from ..db.schema import games as odds_games, odds_snapshots, snapshot_runs, utcnow
from .config import MARKET_STATS
from .metrics import summarize_binary


def grade_pending(engine: Engine, limit: int = 200_000) -> dict:
    e, gr = prop_evaluations.c, prop_grades.c
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(
            select(prop_evaluations, nba_games.c.start_time.label("game_start")).outerjoin(prop_grades, prop_grades.c.evaluation_id == e.id)
            .join(nba_games, nba_games.c.espn_event_id == e.espn_event_id)
            .where(and_(gr.evaluation_id.is_(None), nba_games.c.completed.is_(True), nba_games.c.box_ingested.is_(True))).limit(limit))]
        # Only each quote's LAST pre-tip evaluation is graded (that is what calibration uses);
        # superseded intra-day evaluations stay ungraded so retention can prune them.
        last: dict[str, dict] = {}
        for p in rows:
            if p["game_start"] is not None and p["evaluated_at"] >= p["game_start"]:
                continue
            if p["quote_key"] not in last or p["id"] > last[p["quote_key"]]["id"]:
                last[p["quote_key"]] = p
        already = {r[0] for r in conn.execute(select(e.quote_key).join(prop_grades, prop_grades.c.evaluation_id == e.id)
                                              .where(e.quote_key.in_(list(last) or ["-"])))} if last else set()
        pending = [p for k, p in last.items() if k not in already]
        if not pending:
            return {"graded": 0}
        ev_ids = {p["espn_event_id"] for p in pending}
        box = {(r.espn_event_id, r.athlete_id): dict(r._mapping) for r in conn.execute(
            select(nba_player_games).where(nba_player_games.c.espn_event_id.in_(ev_ids)))}
        tips = {r.provider_event_id: r.commence_time for r in conn.execute(select(odds_games.c.provider_event_id, odds_games.c.commence_time)
                .where(odds_games.c.provider_event_id.in_({p["provider_event_id"] for p in pending})))}
        closing: dict[str, int | None] = {}
        for qk in {p["quote_key"] for p in pending}:
            pev = next(p["provider_event_id"] for p in pending if p["quote_key"] == qk)
            tip = tips.get(pev)
            if tip is None:
                continue
            row = conn.execute(select(odds_snapshots.c.american_odds, odds_snapshots.c.decimal_odds).where(and_(
                odds_snapshots.c.quote_key == qk, odds_snapshots.c.captured_at <= tip)).order_by(odds_snapshots.c.captured_at.desc(),
                odds_snapshots.c.id.desc()).limit(1)).first()
            closing[qk] = (row.american_odds, row.decimal_odds) if row else None
        # CLV needs a capture AFTER the evaluation and before tip; otherwise the "close" is just
        # the evaluated price again and CLV would read 0 — it is recorded as unavailable instead.
        runs = {}
        for prov in {p["provider"] for p in pending}:
            runs[prov] = sorted(r[0] for r in conn.execute(select(snapshot_runs.c.started_at).where(and_(
                snapshot_runs.c.provider == prov, snapshot_runs.c.status.in_(["ok", "partial"])))))
    out = []
    now = utcnow()
    for p in pending:
        tip = tips.get(p["provider_event_id"])
        later = tip is not None and any(p["evaluated_at"] < t <= tip for t in runs.get(p["provider"], ()))
        b = box.get((p["espn_event_id"], p["athlete_id"]))
        if not b or not b["played"]:
            res, actual = "void", None
        else:
            actual = float(sum(b[s] or 0 for s in MARKET_STATS[p["market_code"]]))
            if actual == p["line"]:
                res = "push"
            elif (actual > p["line"]) == (p["side"] == "over"):
                res = "win"
            else:
                res = "loss"
        cl = closing.get(p["quote_key"])
        clv = (1 / cl[1] - 1 / p["decimal_odds"]) if cl and cl[1] and later else None
        out.append(dict(evaluation_id=p["id"], graded_at=now, actual=actual, result=res, closing_american=cl[0] if cl else None, clv_prob=clv))
    with engine.begin() as conn:
        for i in range(0, len(out), 5000):
            conn.execute(insert(prop_grades), out[i:i + 5000])
    return {"graded": len(out), "by_result": {r: sum(1 for x in out if x["result"] == r) for r in ("win", "loss", "push", "void")}}


def live_performance(engine: Engine) -> dict:
    """Calibration on REAL lines: the last pre-tip evaluation of each quote, graded."""
    e, g = prop_evaluations.c, prop_grades.c
    with engine.connect() as conn:
        last = (select(e.quote_key, func.max(e.id).label("mid")).join(prop_grades, g.evaluation_id == e.id).group_by(e.quote_key).subquery())
        rows = [dict(r._mapping) for r in conn.execute(select(prop_evaluations, prop_grades).join(prop_grades, g.evaluation_id == e.id)
                                                       .join(last, last.c.mid == e.id))]
    dec = [r for r in rows if r["result"] in ("win", "loss")]
    res = {"n_graded_quotes": len(rows), "n_decided": len(dec), "n_void": sum(1 for r in rows if r["result"] == "void"),
           "n_push": sum(1 for r in rows if r["result"] == "push")}
    if dec:
        ys = [int(r["result"] == "win") for r in dec]
        res["model"] = summarize_binary([r["model_prob"] / max(1e-9, 1 - (r["model_push_prob"] or 0)) for r in dec], ys)
        nv = [(r["novig_prob"], y) for r, y in zip(dec, ys) if r["novig_prob"] is not None]
        res["market_novig_baseline"] = summarize_binary([a for a, _ in nv], [b for _, b in nv]) if nv else None
        paper = [r for r in dec if r["decision"] == "PAPER_CANDIDATE"]
        res["paper_candidates"] = {"n": len(paper), "wins": sum(1 for r in paper if r["result"] == "win"),
                                   "units": round(sum((r["decimal_odds"] - 1) if r["result"] == "win" else -1 for r in paper), 3),
                                   "mean_clv_prob": round(sum(r["clv_prob"] for r in paper if r["clv_prob"] is not None) /
                                                          max(1, sum(1 for r in paper if r["clv_prob"] is not None)), 4)}
    return res
