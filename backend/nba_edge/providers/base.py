"""Provider interface. The snapshot engine (pipeline/snapshots.py) only talks to this
interface, so adding a source (free or paid) means adding one adapter module — the
storage, diffing, scheduling, budgeting and queries do not change.

An adapter must:
  * never fabricate a price — every Quote comes from a provider response;
  * raise MissingApiKey / AuthError / QuotaExceeded (odds.client) for conditions that
    should stop a run, and report per-event failures in FetchOutcome.failed instead;
  * report each HTTP call to its RequestRecorder (path only — never a key or query);
  * declare how its usage is metered (budget_window + units per call).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from ..odds.normalize import NormalizeResult


@dataclass(frozen=True)
class ProviderEvent:
    event_id: str
    sport_key: str
    commence: datetime
    home: str
    away: str
    espn_event_id: str | None = None
    live: bool = False


@dataclass
class EventOdds:
    event: ProviderEvent
    result: NormalizeResult


@dataclass
class FetchOutcome:
    odds: list[EventOdds] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)       # event id -> reason (non-fatal)
    not_fetched: list[str] = field(default_factory=list)       # skipped (e.g. budget exhausted mid-run)
    notes: list[str] = field(default_factory=list)


class OddsProvider(ABC):
    #: stored in the `provider` column of every row this adapter produces
    name: str = ""
    #: usage metering window for the budget guard: "day" or "month"
    budget_window: str = "month"
    #: environment variable holding this provider's key (for messages only)
    key_env_var: str = ""

    @abstractmethod
    def has_credentials(self) -> bool: ...

    @abstractmethod
    def list_events(self, start: datetime, end: datetime) -> list[ProviderEvent]: ...

    @abstractmethod
    def estimate_units(self, n_events: int) -> int:
        """Upper bound on metered units to fetch `n_events` events' props."""

    @abstractmethod
    def fetch_odds(self, event_ids: list[str], *, max_units: int) -> FetchOutcome: ...

    @abstractmethod
    def market_in_scope(self, market_key: str) -> bool:
        """Was this stored market requested from this provider? Only in-scope markets may
        be tombstoned when absent from a successful fetch."""

    @property
    def requested_bookmakers(self) -> tuple[str, ...]:
        return ()

    def close(self) -> None:  # pragma: no cover - default no-op
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
