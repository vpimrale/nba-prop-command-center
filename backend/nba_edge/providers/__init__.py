"""Odds source registry. Select with ODDS_PROVIDER (default: propline — free)."""
from __future__ import annotations

from ..config import Settings
from .base import EventOdds, FetchOutcome, OddsProvider, ProviderEvent


def make_provider(settings: Settings, *, recorder=None, transport=None, sleep=None, clock=None) -> OddsProvider:
    name = settings.odds_provider
    if name == "propline":
        from .propline import PropLineProvider
        return PropLineProvider(settings, recorder=recorder, transport=transport, sleep=sleep, clock=clock)
    if name == "the-odds-api":
        from .the_odds_api import TheOddsApiProvider
        return TheOddsApiProvider(settings, recorder=recorder, transport=transport, sleep=sleep)
    raise ValueError(f"unknown ODDS_PROVIDER {name!r} (expected 'propline' or 'the-odds-api')")


__all__ = ["make_provider", "OddsProvider", "ProviderEvent", "EventOdds", "FetchOutcome"]
