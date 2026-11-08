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
    # Which odds source to use: "propline" (free tier, default) or "the-odds-api" (paid).
    odds_provider: str = "propline"
    propline_api_key: Secret = field(default_factory=lambda: Secret(None))
    propline_base_url: str = "https://api.prop-line.com/v1"
    # PropLine free tier = 1,000 requests per UTC day; keep headroom for manual use.
    daily_request_budget: int = 800
    # Quotes whose bookmaker feed is older than this (provider's last_update) are stored
    # but flagged stale, and reported as such — never shown as current.
    stale_after_min: int = 20
    odds_api_key: Secret = field(default_factory=lambda: Secret(None))
    odds_api_base_url: str = "https://api.the-odds-api.com"
    sport_key: str = "basketball_nba"
    regions: str = "us"
    # FanDuel + Fanatics are the priority books. NOTE: per The Odds API's bookmaker list
    # (seen via web search, not fetched — see docs), Fanatics is "only available on paid
    # subscriptions", so on a free key it is expected to be absent from responses.
    bookmakers: tuple[str, ...] = ("fanduel", "fanatics")
    # (Add reference books via ODDS_API_BOOKMAKERS, e.g. fanduel,fanatics,draftkings,pinnacle —
    # on PropLine extra books cost no extra requests.)
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
    # Optional extra fixed slots every N minutes between interval_start_et and interval_end_et.
    snapshot_interval_min: int = 0
    interval_start_et: str = "10:00"
    interval_end_et: str = "23:00"
    slot_grace_min: int = 25
    events_refresh_min: int = 60
    # Research jobs inside the scheduler tick (ESPN ingest, injuries, model, grading).
    # Off for bare Settings() (tests); on via from_env unless RESEARCH_JOBS=0.
    research_jobs: bool = False
    espn_ingest_every_min: int = 60
    injuries_every_min: int = 30
    model_every_min: int = 60          # run the slate model at least this often on game days
    model_min_gap_min: int = 20        # ...and after a snapshot, but not more often than this
    grade_every_min: int = 60          # grading also runs right after new final box scores arrive
    # Reliability / operations
    tick_expected_min: int = 5         # the scheduler's intended tick interval (gap detection)
    lock_stale_min: int = 15           # another scheduler's lock is considered dead after this
    maintenance_hour_et: int = 5       # daily prune + backup after this ET hour (off-peak)
    prune_keep_days: int = 3            # superseded intra-day model evaluations (~10k rows/run) are dropped after this
    backup_dir: str = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "backups")
    backup_keep: int = 4               # gzip copies; late in a season each is ~1-2 GB (see docs/OPERATIONS.md)
    # Optional dead-man's-switch URL (e.g. a free healthchecks.io check). Only a bare GET is
    # sent — no data. Treated as a secret: the URL itself is the credential.
    healthcheck_ping_url: Secret = field(default_factory=lambda: Secret(None))

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        base = cls()
        return cls(
            database_url=Secret(env.get("NBA_EDGE_DATABASE_URL") or base.database_url.reveal()),
            odds_provider=(env.get("ODDS_PROVIDER") or base.odds_provider).strip().lower(),
            propline_api_key=Secret(env.get("PROPLINE_API_KEY")),
            propline_base_url=env.get("PROPLINE_BASE_URL") or base.propline_base_url,
            daily_request_budget=_int(env, "ODDS_DAILY_REQUEST_BUDGET", base.daily_request_budget),
            stale_after_min=_int(env, "ODDS_STALE_AFTER_MIN", base.stale_after_min),
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
            snapshot_interval_min=_int(env, "SNAPSHOT_INTERVAL_MIN", base.snapshot_interval_min),
            interval_start_et=env.get("SNAPSHOT_INTERVAL_START_ET") or base.interval_start_et,
            interval_end_et=env.get("SNAPSHOT_INTERVAL_END_ET") or base.interval_end_et,
            events_refresh_min=_int(env, "EVENTS_REFRESH_MIN", base.events_refresh_min),
            research_jobs=(env.get("RESEARCH_JOBS", "1").strip() not in ("0", "false", "no")),
            espn_ingest_every_min=_int(env, "ESPN_INGEST_EVERY_MIN", base.espn_ingest_every_min),
            injuries_every_min=_int(env, "INJURIES_EVERY_MIN", base.injuries_every_min),
            model_every_min=_int(env, "MODEL_EVERY_MIN", base.model_every_min),
            model_min_gap_min=_int(env, "MODEL_MIN_GAP_MIN", base.model_min_gap_min),
            grade_every_min=_int(env, "GRADE_EVERY_MIN", base.grade_every_min),
            tick_expected_min=_int(env, "SCHEDULER_TICK_MIN", base.tick_expected_min),
            lock_stale_min=_int(env, "SCHEDULER_LOCK_STALE_MIN", base.lock_stale_min),
            maintenance_hour_et=_int(env, "MAINTENANCE_HOUR_ET", base.maintenance_hour_et),
            prune_keep_days=_int(env, "PRUNE_KEEP_DAYS", base.prune_keep_days),
            backup_dir=env.get("NBA_EDGE_BACKUP_DIR") or base.backup_dir,
            backup_keep=_int(env, "NBA_EDGE_BACKUP_KEEP", base.backup_keep),
            healthcheck_ping_url=Secret(env.get("HEALTHCHECK_PING_URL")),
        )

    def describe(self) -> dict:
        """Safe-to-print view of the configuration (no secret values)."""
        return {
            "database_url": redact_url(self.database_url.reveal() or ""),
            "odds_provider": self.odds_provider,
            "propline_api_key": "set" if self.propline_api_key else "NOT SET",
            "propline_base_url": self.propline_base_url,
            "daily_request_budget": self.daily_request_budget,
            "stale_after_min": self.stale_after_min,
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
            "snapshot_interval_min": self.snapshot_interval_min,
            "research_jobs": self.research_jobs,
            "tick_expected_min": self.tick_expected_min,
            "maintenance_hour_et": self.maintenance_hour_et,
            "backup_dir": self.backup_dir,
            "backup_keep": self.backup_keep,
            "healthcheck_ping_url": "set" if self.healthcheck_ping_url else "not set",
        }
