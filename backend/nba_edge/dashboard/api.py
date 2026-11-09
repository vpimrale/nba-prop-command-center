"""Read-only JSON views over the database for the local research dashboard."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import and_, desc, func, select
from sqlalchemy.engine import Engine

from ..db.research_schema import (injury_snapshots, model_runs, nba_games, nba_player_games, projections, prop_evaluations)
from ..db.schema import job_runs, odds_current, utcnow
from ..model.backtest import latest_backtest
from ..odds.normalize import game_date_et
from ..queries import status_report
from ..research.ingest import latest_injuries


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def _clean(d: dict) -> dict:
    return {k: _iso(v) for k, v in d.items()}


def latest_run(engine: Engine, status: str = "ok") -> dict | None:
    with engine.connect() as conn:
        r = conn.execute(select(model_runs).where(model_runs.c.status == status).order_by(model_runs.c.id.desc()).limit(1)).mappings().first()
    return _clean(dict(r)) if r else None


def data_status(engine: Engine, now: datetime | None = None) -> dict:
    now = now or utcnow()
    st = status_report(engine, now)
    with engine.connect() as conn:
        jobs = {}
        for name in ("ingest_espn", "ingest_injuries:espn", "ingest_injuries:propline", "refresh_events:propline", "scheduler_tick", "model_run"):
            r = conn.execute(select(job_runs).where(job_runs.c.job_name == name).order_by(job_runs.c.id.desc()).limit(1)).mappings().first()
            if r:
                jobs[name] = {"status": r["status"], "at": _iso(r["started_at"]), "age_min": round((now - r["started_at"]).total_seconds() / 60, 1),
                              "detail": r["detail"]}
        last_box = conn.execute(select(func.max(nba_games.c.date_et)).where(nba_games.c.box_ingested.is_(True))).scalar()
        n_box = conn.execute(select(func.count()).select_from(nba_player_games)).scalar()
    run = latest_run(engine)
    warnings = []
    if run is None:
        warnings.append("No model run yet.")
    else:
        age = (now - datetime.fromisoformat(run["started_at"])).total_seconds() / 60
        if age > 90:
            warnings.append(f"Latest model run is {age:.0f} min old — projections may not reflect current odds/injuries.")
    snap = st.get("last_successful_snapshot")
    if not snap:
        warnings.append("No odds snapshot stored yet.")
    elif snap["age_min"] > 60:
        warnings.append(f"Latest odds snapshot is {snap['age_min']:.0f} min old — prices shown may be stale.")
    if st.get("active_pregame_quotes_flagged_stale"):
        warnings.append(f"{st['active_pregame_quotes_flagged_stale']} pre-game quotes are flagged stale by the provider-freshness check.")
    inj = jobs.get("ingest_injuries:espn") or jobs.get("ingest_injuries:propline")
    if not inj or inj["age_min"] > 120:
        warnings.append("Injury data is older than 2 hours (or missing).")
    return {"as_of": now.isoformat(), "odds": st, "jobs": jobs, "box_scores": {"latest_date_ingested": last_box, "player_rows": n_box},
            "model_run": run, "warnings": warnings}


def slate(engine: Engine, now: datetime | None = None) -> dict:
    now = now or utcnow()
    d = game_date_et(now)
    with engine.connect() as conn:
        gs = [_clean(dict(r._mapping)) for r in conn.execute(select(nba_games).where(nba_games.c.date_et == d).order_by(nba_games.c.start_time))]
    inj = latest_injuries(engine, now)
    teams = {g["home"] for g in gs} | {g["away"] for g in gs}
    by_team: dict[str, list] = {}
    for v in inj.values():
        if v.get("team") in teams:
            by_team.setdefault(v["team"], []).append({"player": v["player_name"], "status": v["status"], "effective": v["effective"],
                                                      "detail": v["detail"], "reported_at": _iso(v["reported_at"]), "source": v["source"]})
    return {"date_et": d, "games": gs, "injuries": by_team}


def props(engine: Engine, run_id: int | None = None, decision: str | None = None, book: str | None = None, limit: int = 200) -> dict:
    run = latest_run(engine) if run_id is None else {"id": run_id}
    if not run:
        return {"run": None, "rows": [], "counts": {}}
    e = prop_evaluations.c
    with engine.connect() as conn:
        counts = dict(conn.execute(select(e.decision, func.count()).where(e.run_id == run["id"]).group_by(e.decision)).all())
        q = select(prop_evaluations).where(e.run_id == run["id"])
        if decision:
            q = q.where(e.decision == decision)
        else:
            q = q.where(e.decision.in_(["PAPER_CANDIDATE", "WATCH"]))
        if book:
            q = q.where(e.bookmaker == book)
        rows = [_clean(dict(r._mapping)) for r in conn.execute(q.order_by(desc(e.decision == "PAPER_CANDIDATE"), desc(e.ev_low)).limit(limit))]
    for r in rows:
        r["reasons"] = json.loads(r["reasons"]) if r.get("reasons") else []
    return {"run": run, "counts": counts, "rows": rows, "no_bet": counts.get("PAPER_CANDIDATE", 0) == 0}


def projection_table(engine: Engine, run_id: int | None = None) -> dict:
    run = latest_run(engine) if run_id is None else {"id": run_id}
    if not run:
        return {"run": None, "players": []}
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(projections).where(projections.c.run_id == run["id"]))]
    players: dict[str, dict] = {}
    for r in rows:
        p = players.setdefault(r["athlete_id"], {k: r[k] for k in ("athlete_id", "player_name", "team", "opponent", "is_home", "p_play",
                                                                   "minutes_mean", "games_this_season", "games_sample", "injury_status", "espn_event_id")})
        p[r["market_code"]] = {k: round(r[k], 2) if isinstance(r[k], float) else r[k] for k in ("mean", "sd", "p10", "median", "p90")}
    return {"run": run, "players": sorted(players.values(), key=lambda p: (p["team"], -(p.get("MIN", {}).get("mean") or 0)))}


def player(engine: Engine, athlete_id: str, run_id: int | None = None) -> dict:
    run = latest_run(engine) if run_id is None else {"id": run_id}
    with engine.connect() as conn:
        logs = [_clean(dict(r._mapping)) for r in conn.execute(select(nba_player_games).where(nba_player_games.c.athlete_id == athlete_id)
                                                               .order_by(nba_player_games.c.start_time.desc()).limit(25))]
        proj = [dict(r._mapping) for r in conn.execute(select(projections).where(and_(projections.c.run_id == (run or {}).get("id"),
                                                                                    projections.c.athlete_id == athlete_id)))] if run else []
        evals = [_clean(dict(r._mapping)) for r in conn.execute(select(prop_evaluations).where(and_(
            prop_evaluations.c.run_id == (run or {}).get("id"), prop_evaluations.c.athlete_id == athlete_id))
            .order_by(prop_evaluations.c.market_code, prop_evaluations.c.line, prop_evaluations.c.bookmaker))] if run else []
        inj = [_clean(dict(r._mapping)) for r in conn.execute(select(injury_snapshots).where(injury_snapshots.c.athlete_id == athlete_id)
                                                              .order_by(injury_snapshots.c.captured_at.desc()).limit(5))]
    notes = next((json.loads(p["notes"]) for p in proj if p["market_code"] == "MIN" and p["notes"]), None)
    dist = {p["market_code"]: {k: p[k] for k in ("mean", "sd", "p10", "p25", "median", "p75", "p90")} for p in proj}
    for ev in evals:
        ev["reasons"] = json.loads(ev["reasons"]) if ev.get("reasons") else []
    return {"athlete_id": athlete_id, "name": logs[0]["player_name"] if logs else None, "logs": logs, "distribution": dist,
            "projection_notes": notes, "evaluations": evals, "injury_reports": inj,
            "hit_rate_note": "Recent hit rates are shown for description only, with sample sizes; they are NOT used as probabilities."}


def paper(engine: Engine) -> dict:
    """REAL track only: the immutable paper ledger + the all-quotes calibration track."""
    from ..model.paper import calibration_track, paper_report
    return {"paper": paper_report(engine), "all_quotes": calibration_track(engine)}


def simulated(engine: Engine) -> dict:
    """SIMULATED track only (walk-forward backtest with synthetic lines). Never merged with paper results."""
    bt = latest_backtest(engine)
    return {"label": "SIMULATED LINES — projection calibration on historical box scores. NOT betting results, NOT paper results.",
            "backtest": bt}


def health(engine: Engine, settings=None) -> dict:
    from ..config import Settings
    from ..pipeline.ops import health_report
    return health_report(engine, settings or Settings())


def recent_runs(engine: Engine, n: int = 20) -> list[dict]:
    since = utcnow() - timedelta(days=7)
    with engine.connect() as conn:
        return [_clean(dict(r._mapping)) for r in conn.execute(select(model_runs).where(model_runs.c.started_at >= since)
                                                               .order_by(model_runs.c.id.desc()).limit(n))]


def odds_for_player(engine: Engine, name: str) -> list[dict]:
    with engine.connect() as conn:
        return [_clean(dict(r._mapping)) for r in conn.execute(select(odds_current).where(and_(odds_current.c.player_name == name,
                                                                                              odds_current.c.market_status == "active")).limit(500))]
