"""Retention for model output (odds snapshots are never pruned — they are the dataset).

A slate run stores ~10k evaluations. Intermediate runs are only useful for a short while;
what calibration needs is each game's LAST pre-tip evaluation. So, for runs older than
`keep_days`: keep evaluations of the last run before each game's tip, keep anything already
graded, delete the rest (and those runs' projection rows except the retained runs')."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, delete, func, select
from sqlalchemy.engine import Engine

from ..db.research_schema import model_runs, nba_games, projections, prop_evaluations, prop_grades
from ..db.schema import utcnow


def prune_evaluations(engine: Engine, keep_days: int = 14) -> dict:
    cutoff = utcnow() - timedelta(days=keep_days)
    e = prop_evaluations.c
    with engine.begin() as conn:
        # last run (by id) evaluated before each game's start
        keep = {r[0] for r in conn.execute(
            select(func.max(e.run_id)).join(nba_games, nba_games.c.espn_event_id == e.espn_event_id)
            .where(e.evaluated_at < nba_games.c.start_time).group_by(e.espn_event_id))}
        old_runs = [r[0] for r in conn.execute(select(model_runs.c.id).where(model_runs.c.started_at < cutoff))]
        prunable = [r for r in old_runs if r not in keep]
        graded = select(prop_grades.c.evaluation_id)
        n_eval = n_proj = 0
        if prunable:
            n_eval = conn.execute(delete(prop_evaluations).where(and_(e.run_id.in_(prunable), e.id.not_in(graded)))).rowcount or 0
            n_proj = conn.execute(delete(projections).where(projections.c.run_id.in_(prunable))).rowcount or 0
    return {"runs_considered": len(old_runs), "runs_retained_for_calibration": len(set(old_runs) & keep),
            "evaluations_deleted": n_eval, "projections_deleted": n_proj}
