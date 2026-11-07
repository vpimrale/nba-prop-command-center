"""Credit budgeting for The Odds API.

Cost model (from the provider's documentation as summarized by web search — NOT verified
against a live account; the pipeline records the provider-REPORTED cost of every call
from the `x-requests-last` header, so real spend is always measured, not assumed):

  event odds call   ≈ markets × region-units, where region-units = number of regions,
                      or ceil(bookmakers / 10) when specific bookmakers are requested.
                      (The docs describe the cost in terms of markets *returned*, so a
                      market a book doesn't offer for that game may cost nothing — the
                      estimate here is therefore an upper bound.)
  events list call  ≈ 0 (documented as not counting against quota; recorded anyway)
  historical calls  ≈ 10 × markets × region-units per event (not used in Stage 1)

Published plans (USD/month, credits/month) as found via web search on 2026-11-07,
UNVERIFIED — confirm on the provider's site before choosing:
  free 500 · $30 20K · $59 100K · $119 5M · $249 15M
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from ..config import Settings
from ..db.schema import api_requests

PUBLISHED_TIERS_UNVERIFIED = [("free", 0, 500), ("20K", 30, 20_000), ("100K", 59, 100_000), ("5M", 119, 5_000_000), ("15M", 249, 15_000_000)]


def region_units(settings: Settings) -> int:
    if settings.bookmakers:
        return max(1, math.ceil(len(settings.bookmakers) / 10))
    return max(1, len([r for r in settings.regions.split(",") if r.strip()]))


def estimate_event_call_cost(settings: Settings) -> int:
    return len(settings.markets) * region_units(settings)


def month_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def window_start(window: str, now: datetime) -> datetime:
    return day_start(now) if window == "day" else month_start(now)


def units_used(conn: Connection, now: datetime, *, provider: str = "the-odds-api", window: str = "month") -> int:
    """Provider-REPORTED usage (credits, or requests) in the current metering window."""
    q = select(func.coalesce(func.sum(api_requests.c.credits_last), 0)).where(
        api_requests.c.requested_at >= window_start(window, now), api_requests.c.provider == provider)
    return int(conn.execute(q).scalar() or 0)


def credits_used_this_month(conn: Connection, now: datetime, provider: str | None = None) -> int:
    q = select(func.coalesce(func.sum(api_requests.c.credits_last), 0)).where(api_requests.c.requested_at >= month_start(now))
    if provider:
        q = q.where(api_requests.c.provider == provider)
    return int(conn.execute(q).scalar() or 0)


def last_reported_remaining(conn: Connection, provider: str | None = None, since: datetime | None = None) -> int | None:
    q = (select(api_requests.c.credits_remaining).where(api_requests.c.credits_remaining.is_not(None))
         .order_by(api_requests.c.requested_at.desc(), api_requests.c.id.desc()).limit(1))
    if provider:
        q = q.where(api_requests.c.provider == provider)
    if since is not None:
        q = q.where(api_requests.c.requested_at >= since)
    return conn.execute(q).scalar()


@dataclass
class BudgetDecision:
    units_allowed: int
    estimate: int
    limit: int
    reason: str

    @property
    def events_allowed(self) -> int:  # backwards-compatible name used in older callers
        return self.units_allowed


def decide(settings: Settings, conn: Connection, provider, n_events: int, now: datetime) -> BudgetDecision:
    """How many metered units this run may spend: the tightest of the per-run cap, the
    configured window budget, and the provider-reported remaining for this window."""
    window = provider.budget_window
    window_budget = settings.daily_request_budget if window == "day" else settings.monthly_credit_budget
    left = window_budget - units_used(conn, now, provider=provider.name, window=window)
    limits = {"per-run cap": settings.max_credits_per_run, f"{window}ly budget".replace("dayly", "daily"): left}
    remaining = last_reported_remaining(conn, provider.name, since=window_start(window, now))
    if remaining is not None:
        limits["provider-reported remaining"] = remaining
    name, limit = min(limits.items(), key=lambda kv: kv[1])
    estimate = provider.estimate_units(n_events)
    allowed = max(0, limit)
    reason = "ok" if allowed >= estimate else f"limited by {name} ({limit} left; estimate {estimate} for {n_events} event(s))"
    return BudgetDecision(allowed, estimate, limit, reason)


def estimate_month(settings: Settings, games_per_day: float, snapshots_per_game: float, days: int = 30) -> dict:
    per_event = estimate_event_call_cost(settings)
    total = math.ceil(per_event * games_per_day * snapshots_per_game * days)
    fits = [f"{n} (${p}/mo, {c:,} credits)" for n, p, c in PUBLISHED_TIERS_UNVERIFIED if c >= total]
    return {
        "markets": len(settings.markets), "region_units": region_units(settings), "credits_per_event_call_upper_bound": per_event,
        "games_per_day": games_per_day, "snapshots_per_game": snapshots_per_game, "days": days,
        "estimated_credits_upper_bound": total,
        "smallest_published_tier_that_fits_UNVERIFIED": fits[0] if fits else "none of the published tiers",
    }


def estimate_day_requests(settings: Settings, games_per_day: float) -> dict:
    """Request-metered providers (PropLine): one bulk call per scheduler run covers the
    near-term slate. Upper bound = fixed/interval slots + one run per pre-tip slot per
    game (simultaneous tips actually share a run) + hourly events refreshes."""
    from .schedule import fixed_slot_times
    fixed = len(fixed_slot_times(settings))
    pretip = math.ceil(games_per_day * len(settings.pretip_offsets_min))
    refresh = math.ceil(24 * 60 / max(1, settings.events_refresh_min))
    total = fixed + pretip + refresh
    return {"provider": settings.odds_provider, "fixed_and_interval_runs": fixed, "pretip_runs_upper_bound": pretip,
            "events_refreshes_upper_bound": refresh, "requests_per_day_upper_bound": total,
            "configured_daily_budget": settings.daily_request_budget,
            "fits_daily_budget": total <= settings.daily_request_budget}
