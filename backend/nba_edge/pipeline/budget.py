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


def credits_used_this_month(conn: Connection, now: datetime) -> int:
    q = select(func.coalesce(func.sum(api_requests.c.credits_last), 0)).where(api_requests.c.requested_at >= month_start(now))
    return int(conn.execute(q).scalar() or 0)


def last_reported_remaining(conn: Connection) -> int | None:
    q = (select(api_requests.c.credits_remaining).where(api_requests.c.credits_remaining.is_not(None))
         .order_by(api_requests.c.requested_at.desc(), api_requests.c.id.desc()).limit(1))
    return conn.execute(q).scalar()


@dataclass
class BudgetDecision:
    events_allowed: int
    per_event_estimate: int
    limit: int
    reason: str


def decide(settings: Settings, conn: Connection, n_events: int, now: datetime) -> BudgetDecision:
    per_event = estimate_event_call_cost(settings)
    month_left = settings.monthly_credit_budget - credits_used_this_month(conn, now)
    provider_left = last_reported_remaining(conn)
    limits = {"per-run cap": settings.max_credits_per_run, "monthly budget": month_left}
    if provider_left is not None:
        limits["provider-reported remaining"] = provider_left
    name, limit = min(limits.items(), key=lambda kv: kv[1])
    allowed = 0 if per_event <= 0 else max(0, min(n_events, limit // per_event))
    reason = "ok" if allowed >= n_events else f"limited by {name} ({limit} credits left; ~{per_event}/event)"
    return BudgetDecision(allowed, per_event, limit, reason)


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
