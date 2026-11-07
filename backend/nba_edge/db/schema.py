"""Database schema (SQLAlchemy Core; portable across SQLite for local/test and Postgres
for production).

Design for the historical odds dataset:

* `odds_snapshots` is APPEND-ONLY. A row is written whenever a quote is first seen, its
  price changes, it disappears ('removed' tombstone), or it reappears. Rows are never
  updated or deleted by the pipeline. The price of any quote at any moment T is the
  latest row for its `quote_key` with captured_at <= T — and if that row is a
  'removed' tombstone, the quote was not on offer at T.
* `odds_current` is a mutable cache of the latest state per quote, used only to diff a
  new fetch against. It can always be rebuilt from `odds_snapshots`.
* `snapshot_runs` records every attempted capture (including skipped/blocked/failed ones)
  and `api_requests` records every provider call with the provider-reported credit
  cost, so data freshness and spend are auditable. No API key or query string is stored.

Change-only storage keeps the dataset compact (a season of every-price-every-run rows
would be tens of millions) while losing no information about what was offered when.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, MetaData, String, Table, Text,
                        UniqueConstraint)
from sqlalchemy.types import TypeDecorator

SCHEMA_VERSION = 2  # v2: provenance columns (provider_player_id, line_type, outcome_label, source_*_at, stale)


class UTCDateTime(TypeDecorator):
    """Always timezone-aware UTC in Python; stored as UTC on every backend."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to UTCDateTime")
        value = value.astimezone(timezone.utc)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


metadata = MetaData()

schema_info = Table(
    "schema_info", metadata,
    Column("id", Integer, primary_key=True),
    Column("version", Integer, nullable=False),
    Column("applied_at", UTCDateTime, nullable=False),
)

games = Table(
    "games", metadata,
    Column("provider", String(40), primary_key=True),
    Column("provider_event_id", String(64), primary_key=True),
    Column("sport_key", String(40), nullable=False),
    Column("commence_time", UTCDateTime, nullable=False),
    Column("game_date_et", String(10), nullable=False, index=True),
    Column("home_team", String(80), nullable=False),
    Column("away_team", String(80), nullable=False),
    Column("first_seen_at", UTCDateTime, nullable=False),
    Column("last_seen_at", UTCDateTime, nullable=False),
    Column("espn_event_id", String(32)),  # joined in Stage 2
)

snapshot_runs = Table(
    "snapshot_runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("slot_key", String(120), index=True),     # scheduler slot this run served (None for manual runs)
    Column("reason", String(40), nullable=False),     # fixed | pretip | closing | manual
    Column("provider", String(40)),                   # v2
    Column("started_at", UTCDateTime, nullable=False),
    Column("finished_at", UTCDateTime),
    # running | ok | partial | failed | blocked_no_key | blocked_auth | skipped_budget | no_events | missed
    Column("status", String(24), nullable=False),
    Column("events_planned", Integer, nullable=False, default=0),
    Column("events_fetched", Integer, nullable=False, default=0),
    Column("events_failed", Integer, nullable=False, default=0),
    Column("quotes_seen", Integer, nullable=False, default=0),
    Column("rows_inserted", Integer, nullable=False, default=0),
    Column("rows_removed", Integer, nullable=False, default=0),
    Column("outcomes_skipped", Integer, nullable=False, default=0),
    Column("quotes_stale", Integer),                  # v2: quotes flagged stale in this run
    Column("credits_estimated", Integer),
    Column("credits_reported", Integer),   # sum of provider x-requests-last for this run
    Column("credits_remaining", Integer),  # last provider x-requests-remaining seen
    Column("bookmakers_seen", String(200)),
    Column("detail", Text),
)

# Generic job log (events refreshes, scheduler ticks) for "scheduled job health".
job_runs = Table(
    "job_runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_name", String(60), nullable=False, index=True),
    Column("started_at", UTCDateTime, nullable=False),
    Column("finished_at", UTCDateTime),
    Column("status", String(24), nullable=False),
    Column("detail", Text),
)

# One row per scheduler slot that was served (or missed) — prevents double-spending
# credits on a slot and makes missed closing snapshots visible.
slot_completions = Table(
    "slot_completions", metadata,
    Column("slot_key", String(160), primary_key=True),
    Column("run_id", Integer, ForeignKey("snapshot_runs.id")),
    Column("completed_at", UTCDateTime, nullable=False),
    Column("status", String(24), nullable=False),  # ok | partial | failed | missed | blocked_no_key | ...
)

api_requests = Table(
    "api_requests", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, ForeignKey("snapshot_runs.id")),
    Column("requested_at", UTCDateTime, nullable=False, index=True),
    Column("provider", String(40), nullable=False),
    Column("path", String(200), nullable=False),  # path only — never the query string / key
    Column("status_code", Integer),
    Column("duration_ms", Integer),
    Column("credits_last", Integer),
    Column("credits_used", Integer),
    Column("credits_remaining", Integer),
    Column("error", Text),
)

_quote_cols = lambda: [  # noqa: E731 — shared column set for snapshots + current
    Column("quote_key", String(400), nullable=False),
    Column("provider", String(40), nullable=False),
    Column("sport_key", String(40), nullable=False),
    Column("provider_event_id", String(64), nullable=False),
    Column("commence_time", UTCDateTime),
    Column("game_date_et", String(10)),
    Column("home_team", String(80)),
    Column("away_team", String(80)),
    Column("bookmaker", String(40), nullable=False),
    Column("market_key", String(80), nullable=False),
    Column("market_code", String(8)),
    Column("is_alternate", Boolean, nullable=False),
    Column("player_name", String(120), nullable=False),
    Column("player_team", String(8)),   # resolved in Stage 2 (odds feed gives name only)
    Column("opponent", String(8)),      # resolved in Stage 2
    Column("side", String(16), nullable=False),
    Column("line", Float),
    Column("american_odds", Integer),
    Column("decimal_odds", Float),
    Column("market_status", String(12), nullable=False),  # active | removed
    # v2 provenance (nullable; populated by providers that expose them)
    Column("provider_player_id", String(40)),   # e.g. "nba:<personId>" (NBA CDN personId)
    Column("line_type", String(20)),            # provider label: main | alternate | milestone
    Column("outcome_label", String(80)),        # original outcome text, e.g. "10+ Points"
    Column("source_seen_at", UTCDateTime),      # provider's last sighting of the outcome
    Column("source_changed_at", UTCDateTime),   # provider's last observed price change
    Column("stale", Boolean),                   # provider feed older than the freshness threshold
]

odds_snapshots = Table(
    "odds_snapshots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, ForeignKey("snapshot_runs.id"), nullable=False),
    Column("captured_at", UTCDateTime, nullable=False),
    Column("snapshot_reason", String(40), nullable=False),
    Column("change_type", String(16), nullable=False),  # new | price_change | removed | reappeared
    Column("prev_american_odds", Integer),
    Column("book_last_update", UTCDateTime),
    Column("market_last_update", UTCDateTime),
    Column("model_projection", Float),   # filled once projections exist (Stage 3+)
    Column("model_version", String(40)),
    *_quote_cols(),
    Index("ix_snap_key_time", "quote_key", "captured_at"),
    Index("ix_snap_event_time", "provider_event_id", "captured_at"),
    Index("ix_snap_date", "game_date_et"),
)

odds_current = Table(
    "odds_current", metadata,
    *_quote_cols(),
    Column("first_seen_at", UTCDateTime, nullable=False),
    Column("last_seen_at", UTCDateTime, nullable=False),
    Column("last_changed_at", UTCDateTime, nullable=False),
    Column("last_run_id", Integer, nullable=False),
    UniqueConstraint("quote_key", name="uq_current_key"),
    Index("ix_current_event_book", "provider_event_id", "bookmaker"),
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
