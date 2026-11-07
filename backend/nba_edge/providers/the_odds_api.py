"""Adapter: The Odds API (paid source; free tier = 500 credits/month, Fanatics paid-only).
Kept so a paid plan can be switched on later with ODDS_PROVIDER=the-odds-api."""
from __future__ import annotations

from datetime import datetime, timezone

from ..config import Settings
from ..odds.client import NotFound, OddsApiClient, OddsApiError
from ..odds.normalize import normalize_event_odds, parse_ts
from ..pipeline import budget
from .base import EventOdds, FetchOutcome, OddsProvider, ProviderEvent

NAME = "the-odds-api"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TheOddsApiProvider(OddsProvider):
    name = NAME
    budget_window = "month"
    key_env_var = "ODDS_API_KEY"

    def __init__(self, settings: Settings, *, recorder=None, transport=None, sleep=None):
        self.settings = settings
        kwargs = {"transport": transport, "on_request": recorder}
        if sleep is not None:
            kwargs["sleep"] = sleep
        self.client = OddsApiClient(settings, **kwargs)

    def has_credentials(self) -> bool:
        return bool(self.settings.odds_api_key)

    @property
    def requested_bookmakers(self) -> tuple[str, ...]:
        return self.settings.bookmakers

    def list_events(self, start: datetime, end: datetime) -> list[ProviderEvent]:
        out = []
        for ev in self.client.list_events(_iso(start), _iso(end)).data or []:
            commence = parse_ts(ev.get("commence_time"))
            if ev.get("id") and commence:
                out.append(ProviderEvent(str(ev["id"]), ev.get("sport_key", self.settings.sport_key), commence,
                                         ev.get("home_team", ""), ev.get("away_team", "")))
        return out

    def estimate_units(self, n_events: int) -> int:
        return budget.estimate_event_call_cost(self.settings) * n_events

    def fetch_odds(self, event_ids: list[str], *, max_units: int) -> FetchOutcome:
        out = FetchOutcome()
        per_event = budget.estimate_event_call_cost(self.settings)
        spent = 0
        for ev_id in event_ids:
            if spent + per_event > max_units:
                out.not_fetched.append(ev_id)
                continue
            try:
                resp = self.client.event_odds(ev_id, self.settings.markets)
            except NotFound as e:
                out.failed[ev_id] = f"not found ({e})"
                continue
            except OddsApiError as e:
                if type(e).__name__ in ("AuthError", "QuotaExceeded", "MissingApiKey"):
                    raise
                out.failed[ev_id] = str(e)
                continue
            spent += per_event
            payload = resp.data or {}
            if str(payload.get("id", ev_id)) != ev_id:
                out.failed[ev_id] = f"response was for a different event ({payload.get('id')}) — ignored"
                continue
            commence = parse_ts(payload.get("commence_time"))
            event = ProviderEvent(ev_id, payload.get("sport_key", self.settings.sport_key), commence,
                                  payload.get("home_team", ""), payload.get("away_team", "")) if commence else None
            out.odds.append(EventOdds(event or ProviderEvent(ev_id, self.settings.sport_key, None, "", ""),  # type: ignore[arg-type]
                                      normalize_event_odds({**payload, "id": ev_id}, provider=NAME)))
        return out

    def market_in_scope(self, market_key: str) -> bool:
        return market_key in self.settings.markets

    def close(self) -> None:
        self.client.close()
