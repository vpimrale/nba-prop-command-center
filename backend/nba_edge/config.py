"""Runtime configuration, read from environment variables only.

Secrets (the odds API key, a database URL with a password) are wrapped in `Secret`,
whose repr/str never reveal the value, so they cannot leak through logging, tracebacks
of dataclass reprs, or CLI output. Nothing here has a default key: with no key set the
pipeline refuses to call the provider and reports "blocked" instead.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .odds.markets import DEFAULT_MARKET_KEYS


class Secret:
    """A string that never prints itself."""

    __slots__ = ("_value",)

    def __init__(self, value: str | None):
        self._value = value or None

    def reveal(self) -> str | None:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Secret(<set>)" if self._value else "Secret(<unset>)"

    __str__ = __repr__


def _csv(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default
    return tuple(v.strip() for v in value.split(",") if v.strip())


def _int(env: dict, key: str, default: int) -> int:
    raw = env.get(key)
    return int(raw) if raw not in (None, "") else default


def redact_url(url: str) -> str:
    """Hide credentials in a DB URL or apiKey in a provider URL (for display only)."""
    url = re.sub(r"(://[^:/@]+:)[^@]+@", r"\1***@", url)
    return re.sub(r"(?i)(apiKey=)[^&\s]+", r"\1***", url)


DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "nba_edge.sqlite3")


@dataclass(frozen=True)
class Settings:
    database_url: Secret = field(default_factory=lambda: Secret(f"sqlite:///{DEFAULT_DB_PATH}"))
    odds_api_key: Secret = field(default_factory=lambda: Secret(None))
    odds_api_base_url: str = "https://api.the-odds-api.com"
    sport_key: str = "basketball_nba"
    regions: str = "us"
    # FanDuel + Fanatics are the priority books. NOTE: per The Odds API's bookmaker list
    # (seen via web search, not fetched — see docs), Fanatics is "only available on paid
    # subscriptions", so on a free key it is expected to be absent from responses.
    bookmakers: tuple[str, ...] = ("fanduel", "fanatics")
    markets: tuple[str, ...] = DEFAULT_MARKET_KEYS
    # Credit controls. These are LOCAL guards against overspending a plan the user has
    # chosen; they purchase nothing. monthly_credit_budget should be set to the plan's
    # monthly allowance (or lower).
    monthly_credit_budget: int = 500   # = the free tier; raise only to match a plan you have approved
    max_credits_per_run: int = 300
    request_timeout_s: float = 20.0
    max_retries: int = 3
    # Snapshot schedule (US Eastern). Fixed daily slots for the whole slate, plus
    # per-game pre-tip slots (minutes before tip-off). The last pre-tip slot is the
    # "closing" snapshot used for closing-line value.
    fixed_slots_et: tuple[str, ...] = ("10:00", "15:00")
    pretip_offsets_min: tuple[int, ...] = (60, 10)
    slot_grace_min: int = 25
    events_refresh_min: int = 60

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        base = cls()
        return cls(
            database_url=Secret(env.get("NBA_EDGE_DATABASE_URL") or base.database_url.reveal()),
            odds_api_key=Secret(env.get("ODDS_API_KEY")),
            odds_api_base_url=env.get("ODDS_API_BASE_URL") or base.odds_api_base_url,
            sport_key=env.get("ODDS_API_SPORT") or base.sport_key,
            regions=env.get("ODDS_API_REGIONS") or base.regions,
            bookmakers=_csv(env.get("ODDS_API_BOOKMAKERS"), base.bookmakers),
            markets=_csv(env.get("ODDS_API_MARKETS"), base.markets),
            monthly_credit_budget=_int(env, "ODDS_MONTHLY_CREDIT_BUDGET", base.monthly_credit_budget),
            max_credits_per_run=_int(env, "ODDS_MAX_CREDITS_PER_RUN", base.max_credits_per_run),
            request_timeout_s=float(env.get("ODDS_REQUEST_TIMEOUT_S") or base.request_timeout_s),
            max_retries=_int(env, "ODDS_MAX_RETRIES", base.max_retries),
            fixed_slots_et=_csv(env.get("SNAPSHOT_FIXED_SLOTS_ET"), base.fixed_slots_et),
            pretip_offsets_min=tuple(int(x) for x in _csv(env.get("SNAPSHOT_PRETIP_OFFSETS_MIN"), tuple(map(str, base.pretip_offsets_min)))),
            slot_grace_min=_int(env, "SNAPSHOT_SLOT_GRACE_MIN", base.slot_grace_min),
            events_refresh_min=_int(env, "EVENTS_REFRESH_MIN", base.events_refresh_min),
        )

    def describe(self) -> dict:
        """Safe-to-print view of the configuration (no secret values)."""
        return {
            "database_url": redact_url(self.database_url.reveal() or ""),
            "odds_api_key": "set" if self.odds_api_key else "NOT SET",
            "odds_api_base_url": self.odds_api_base_url,
            "sport_key": self.sport_key,
            "regions": self.regions,
            "bookmakers": list(self.bookmakers),
            "markets": list(self.markets),
            "monthly_credit_budget": self.monthly_credit_budget,
            "max_credits_per_run": self.max_credits_per_run,
            "fixed_slots_et": list(self.fixed_slots_et),
            "pretip_offsets_min": list(self.pretip_offsets_min),
        }
