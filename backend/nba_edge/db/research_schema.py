"""Research + model tables (schema v3). Registered on the shared metadata so init_db
creates/migrates them with everything else.

Data provenance:
  nba_games / nba_player_games / nba_team_games  <- ESPN public box scores (site.api.espn.com)
  injury_snapshots                              <- ESPN injuries feed + PropLine (ESPN-sourced)
  model_runs / projections / prop_evaluations   <- this project's model (never a provider's)
  prop_grades                                   <- evaluations graded against final box scores
  backtest_runs / backtest_rows                 <- walk-forward projection backtests on
                                                   historical box scores with SIMULATED lines
"""
from __future__ import annotations

from sqlalchemy import Boolean, Column, Float, ForeignKey, Index, Integer, String, Table, Text

from .schema import UTCDateTime, metadata

nba_games = Table(
    "nba_games", metadata,
    Column("espn_event_id", String(20), primary_key=True),
    Column("season", Integer, nullable=False),        # ESPN season year: 2027 = 2026-27
    Column("season_type", Integer, nullable=False),   # 1 pre, 2 regular, 3 post, 5 play-in
    Column("date_et", String(10), nullable=False, index=True),
    Column("start_time", UTCDateTime),
    Column("home", String(10), nullable=False),        # ESPN abbreviations (GS, NY, SA, NO, UTAH, WSH, ...)
    Column("away", String(10), nullable=False),
    Column("home_score", Integer),
    Column("away_score", Integer),
    Column("status", String(30)),
    Column("completed", Boolean, nullable=False, default=False),
    Column("box_ingested", Boolean, nullable=False, default=False),
    Column("updated_at", UTCDateTime, nullable=False),
)

nba_player_games = Table(
    "nba_player_games", metadata,
    Column("espn_event_id", String(20), primary_key=True),
    Column("athlete_id", String(20), primary_key=True),
    Column("player_name", String(120), nullable=False),
    Column("position", String(10)),
    Column("team", String(10), nullable=False),
    Column("opponent", String(10), nullable=False),
    Column("is_home", Boolean, nullable=False),
    Column("starter", Boolean),
    Column("played", Boolean, nullable=False),        # recorded >0 minutes
    Column("dnp_reason", String(80)),
    Column("minutes", Float, nullable=False),
    Column("pts", Integer), Column("reb", Integer), Column("ast", Integer),
    Column("fg3m", Integer), Column("fg3a", Integer), Column("fgm", Integer), Column("fga", Integer),
    Column("ftm", Integer), Column("fta", Integer), Column("oreb", Integer), Column("dreb", Integer),
    Column("stl", Integer), Column("blk", Integer), Column("tov", Integer), Column("pf", Integer),
    Column("plus_minus", Integer),
    Column("season", Integer, nullable=False),
    Column("season_type", Integer, nullable=False),
    Column("date_et", String(10), nullable=False),
    Column("start_time", UTCDateTime),
    Index("ix_pg_athlete_time", "athlete_id", "start_time"),
    Index("ix_pg_team_date", "team", "date_et"),
)

nba_team_games = Table(
    "nba_team_games", metadata,
    Column("espn_event_id", String(20), primary_key=True),
    Column("team", String(10), primary_key=True),
    Column("opponent", String(10), nullable=False),
    Column("is_home", Boolean, nullable=False),
    Column("pts", Integer), Column("opp_pts", Integer),
    Column("fga", Integer), Column("fta", Integer), Column("oreb", Integer), Column("tov", Integer),
    Column("reb", Integer), Column("ast", Integer), Column("fg3m", Integer),
    Column("minutes", Float),            # game length in minutes (48 + 5 per OT)
    Column("possessions", Float),        # FGA + 0.44*FTA - OREB + TOV (team estimate)
    Column("season", Integer, nullable=False),
    Column("season_type", Integer, nullable=False),
    Column("date_et", String(10), nullable=False),
)

injury_snapshots = Table(
    "injury_snapshots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("captured_at", UTCDateTime, nullable=False, index=True),
    Column("source", String(30), nullable=False),     # espn | propline(espn)
    Column("athlete_id", String(20)),                 # ESPN athlete id when known
    Column("player_name", String(120), nullable=False),
    Column("team", String(10)),
    Column("status", String(40), nullable=False),     # Out | Doubtful | Questionable | Probable | Day-To-Day | ...
    Column("detail", Text),
    Column("reported_at", UTCDateTime),
)

player_xref = Table(
    "player_xref", metadata,
    Column("provider", String(40), primary_key=True),
    Column("provider_player_key", String(160), primary_key=True),  # provider player id, or normalized name|team
    Column("provider_name", String(120)),
    Column("espn_athlete_id", String(20)),
    Column("method", String(30), nullable=False),     # name_team | name_only | unmatched
    Column("matched_at", UTCDateTime, nullable=False),
)

model_runs = Table(
    "model_runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("started_at", UTCDateTime, nullable=False),
    Column("finished_at", UTCDateTime),
    Column("model_version", String(40), nullable=False),
    Column("slate_date_et", String(10), nullable=False, index=True),
    Column("status", String(24), nullable=False),
    Column("n_games", Integer), Column("n_players", Integer), Column("n_projections", Integer),
    Column("n_evaluations", Integer), Column("n_candidates", Integer),
    Column("sims_per_player", Integer),
    Column("odds_as_of", UTCDateTime),
    Column("detail", Text),
)

projections = Table(
    "projections", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, ForeignKey("model_runs.id"), nullable=False, index=True),
    Column("espn_event_id", String(20), nullable=False),
    Column("athlete_id", String(20), nullable=False),
    Column("player_name", String(120), nullable=False),
    Column("team", String(10), nullable=False),
    Column("opponent", String(10), nullable=False),
    Column("is_home", Boolean, nullable=False),
    Column("market_code", String(8), nullable=False),  # MIN, PTS, REB, AST, 3PM, STL, BLK, TO, PRA, PR, PA, RA
    Column("p_play", Float, nullable=False),
    Column("mean", Float, nullable=False),            # conditional on playing
    Column("sd", Float, nullable=False),
    Column("p10", Float), Column("p25", Float), Column("median", Float), Column("p75", Float), Column("p90", Float),
    Column("minutes_mean", Float),
    Column("games_this_season", Integer),
    Column("games_sample", Integer),                  # games behind the estimate (both seasons)
    Column("injury_status", String(40)),
    Column("notes", Text),                            # JSON: inputs and adjustments, for research view
    Index("ix_proj_run_player", "run_id", "athlete_id"),
)

prop_evaluations = Table(
    "prop_evaluations", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, ForeignKey("model_runs.id"), nullable=False, index=True),
    Column("evaluated_at", UTCDateTime, nullable=False),
    Column("quote_key", String(400), nullable=False),
    Column("provider", String(40), nullable=False),
    Column("bookmaker", String(40), nullable=False),
    Column("provider_event_id", String(64), nullable=False),
    Column("espn_event_id", String(20)),
    Column("athlete_id", String(20)),
    Column("player_name", String(120), nullable=False),
    Column("market_key", String(80), nullable=False),
    Column("market_code", String(8), nullable=False),
    Column("is_alternate", Boolean, nullable=False),
    Column("side", String(16), nullable=False),
    Column("line", Float),
    Column("american_odds", Integer, nullable=False),
    Column("decimal_odds", Float, nullable=False),
    Column("quote_seen_at", UTCDateTime),
    Column("implied_prob", Float, nullable=False),
    Column("novig_prob", Float),
    Column("novig_method", String(30)),               # two_way | book_margin_approx | none
    Column("model_prob", Float),                      # P(win | plays)
    Column("model_push_prob", Float),
    Column("model_prob_sd", Float),                   # between-batch (parameter) uncertainty
    Column("model_prob_low", Float),                  # conservative bound used by the gate
    Column("p_play", Float),
    Column("fair_american", Integer),
    Column("ev", Float),                              # expected profit per 1 unit staked (void if DNP)
    Column("ev_low", Float),                          # EV at model_prob_low
    Column("edge_vs_novig", Float),
    Column("confidence", String(10)),                 # low | medium | high
    Column("decision", String(20), nullable=False),   # NO_BET | WATCH | PAPER_CANDIDATE
    Column("reasons", Text),                          # JSON list of gate results
    Column("model_version", String(40), nullable=False),
    Index("ix_eval_run_decision", "run_id", "decision"),
    Index("ix_eval_quote", "quote_key"),
)

prop_grades = Table(
    "prop_grades", metadata,
    Column("evaluation_id", Integer, ForeignKey("prop_evaluations.id"), primary_key=True),
    Column("graded_at", UTCDateTime, nullable=False),
    Column("actual", Float),
    Column("result", String(10), nullable=False),     # win | loss | push | void
    Column("closing_american", Integer),              # last pre-tip snapshot price of the same quote
    Column("clv_prob", Float),                        # implied(closing) - implied(at evaluation)
)

backtest_runs = Table(
    "backtest_runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", UTCDateTime, nullable=False),
    Column("model_version", String(40), nullable=False),
    Column("date_from", String(10), nullable=False),
    Column("date_to", String(10), nullable=False),
    Column("line_method", String(200), nullable=False),  # e.g. "SIMULATED: floor(prior L10 mean) + 0.5 — ..."
    Column("n_rows", Integer),
    Column("summary", Text),                          # JSON metrics
)

backtest_rows = Table(
    "backtest_rows", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("backtest_id", Integer, ForeignKey("backtest_runs.id"), nullable=False, index=True),
    Column("date_et", String(10), nullable=False),
    Column("athlete_id", String(20), nullable=False),
    Column("market_code", String(8), nullable=False),
    Column("line", Float, nullable=False),            # SIMULATED line
    Column("p_over", Float, nullable=False),
    Column("p_push", Float, nullable=False),
    Column("mean", Float, nullable=False),
    Column("actual", Float, nullable=False),
    Column("pit", Float),                             # P(X < actual) + 0.5 P(X = actual)
)
