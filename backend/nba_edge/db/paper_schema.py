"""Paper-trading ledger (schema v4) — the REAL-lines track, kept apart from simulated backtests.

paper_recommendations : one row per quote the model flagged PAPER_CANDIDATE, frozen at the
                        moment it first qualified (exact line, price, projection, probability,
                        EV, model version, timestamp). Insert-only.
paper_outcomes        : one row per recommendation once its game is final (actual stat,
                        result, paper profit at the RECOMMENDATION price, closing price, CLV).
                        Insert-only.
Both are protected by database triggers that reject UPDATE and DELETE, so the record cannot
be edited after the fact by this code or a stray script (a database owner can still drop a
trigger — tamper-resistant, not tamper-proof; the daily backups are the second line). Each recommendation also
carries a SHA-256 of its canonical contents (tamper evidence for exports).

No money is involved anywhere: stakes are notional 1-unit paper stakes.
"""
from __future__ import annotations

from sqlalchemy import Boolean, Column, Float, ForeignKey, Index, Integer, String, Table, Text, text
from sqlalchemy.engine import Engine

from .schema import UTCDateTime, metadata

paper_recommendations = Table(
    "paper_recommendations", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("rec_key", String(460), nullable=False, unique=True),   # model_version|quote_key: first qualification only
    Column("recommended_at", UTCDateTime, nullable=False, index=True),
    Column("run_id", Integer, nullable=False),
    Column("model_version", String(40), nullable=False),
    Column("slate_date_et", String(10), nullable=False),
    Column("espn_event_id", String(20), nullable=False),
    Column("commence_time", UTCDateTime),
    Column("provider", String(40), nullable=False),
    Column("provider_event_id", String(64), nullable=False),
    Column("bookmaker", String(40), nullable=False),
    Column("quote_key", String(400), nullable=False),
    Column("athlete_id", String(20), nullable=False),
    Column("player_name", String(120), nullable=False),
    Column("team", String(10)),
    Column("opponent", String(10)),
    Column("market_key", String(80), nullable=False),
    Column("market_code", String(8), nullable=False),
    Column("is_alternate", Boolean, nullable=False),
    Column("side", String(16), nullable=False),
    Column("line", Float, nullable=False),
    Column("american_odds", Integer, nullable=False),
    Column("decimal_odds", Float, nullable=False),
    Column("quote_seen_at", UTCDateTime),
    Column("odds_snapshot_id", Integer),            # the append-only odds_snapshots row that holds this price
    Column("projection_mean", Float), Column("projection_median", Float), Column("projection_sd", Float),
    Column("minutes_mean", Float),
    Column("p_play", Float, nullable=False),
    Column("model_prob", Float, nullable=False),
    Column("model_push_prob", Float, nullable=False),
    Column("model_prob_sd", Float),
    Column("model_prob_low", Float),
    Column("implied_prob", Float, nullable=False),
    Column("novig_prob", Float),
    Column("novig_method", String(30)),
    Column("fair_american", Integer),
    Column("ev", Float, nullable=False),
    Column("ev_low", Float),
    Column("confidence", String(10)),
    Column("gates", Text),
    Column("stake_units", Float, nullable=False, default=1.0),   # notional paper stake
    Column("content_sha256", String(64), nullable=False),
    Index("ix_paper_rec_event", "espn_event_id"),
)

paper_outcomes = Table(
    "paper_outcomes", metadata,
    Column("recommendation_id", Integer, ForeignKey("paper_recommendations.id"), primary_key=True),
    Column("graded_at", UTCDateTime, nullable=False),
    Column("actual", Float),                        # final stat (None if the player did not play)
    Column("result", String(10), nullable=False),   # win | loss | push | void
    Column("profit_units", Float, nullable=False),  # at the RECOMMENDATION price, 1-unit stake
    Column("closing_american", Integer),            # last pre-tip snapshot of the same quote (None if never seen)
    Column("closing_decimal", Float),
    Column("closing_captured_at", UTCDateTime),
    Column("closing_minutes_before_tip", Float),
    Column("closing_offered", Boolean),             # False if the last pre-tip state was "removed"
    Column("clv_prob", Float),                      # implied(close) - implied(recommendation); >0 = beat the close
    Column("outcome_source", String(80), nullable=False),
)

scheduler_lock = Table(
    "scheduler_lock", metadata,
    Column("name", String(40), primary_key=True),
    Column("holder", String(120), nullable=False),
    Column("heartbeat_at", UTCDateTime, nullable=False),
)

# prop_grades (the all-quotes real-line calibration track) is append-only too. prop_evaluations
# is NOT: retention prunes superseded intra-day evaluations (see model/retention.py).
IMMUTABLE = {"paper_recommendations": ("UPDATE", "DELETE"), "paper_outcomes": ("UPDATE", "DELETE"),
             "prop_grades": ("UPDATE", "DELETE")}


def install_immutability_triggers(engine: Engine) -> None:
    """Idempotently install triggers that make the paper-trading record append-only."""
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            for table, ops in IMMUTABLE.items():
                for op in ops:
                    conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_{op.lower()} BEFORE {op} ON {table} "
                                      f"BEGIN SELECT RAISE(ABORT, '{table} is append-only ({op} rejected)'); END;"))
        elif engine.dialect.name == "postgresql":
            conn.execute(text("""CREATE OR REPLACE FUNCTION nba_edge_append_only() RETURNS trigger AS $$
                BEGIN RAISE EXCEPTION '% is append-only (% rejected)', TG_TABLE_NAME, TG_OP; END; $$ LANGUAGE plpgsql;"""))
            for table, ops in IMMUTABLE.items():
                conn.execute(text(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}"))
                conn.execute(text(f"CREATE TRIGGER trg_{table}_append_only BEFORE {' OR '.join(ops)} ON {table} "
                                  f"FOR EACH ROW EXECUTE FUNCTION nba_edge_append_only()"))
