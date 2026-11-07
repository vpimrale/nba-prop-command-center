"""The Odds API v4 client (https://the-odds-api.com/liveapi/guides/v4/).

Endpoints used (documented names; behavior not yet verified live — see README):
  GET /v4/sports/{sport}/events                    -> upcoming events (ids, teams, tip-off)
  GET /v4/sports/{sport}/events/{eventId}/odds     -> one event's odds for chosen markets/books

Every response's usage headers (x-requests-last / -used / -remaining) are captured so
the ACTUAL credit cost is recorded, rather than trusting an estimate. The key travels as
the `apiKey` query parameter (the provider's scheme); it is never logged, never stored,
and the URL recorded for a request has its query string removed.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from ..config import Settings
from ..logging_setup import register_secret, scrub

log = logging.getLogger(__name__)


class OddsApiError(Exception):
    """Base class. `retryable` errors were already retried before being raised."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(scrub(message))
        self.status = status
        self.retryable = retryable


class MissingApiKey(OddsApiError):
    pass


class AuthError(OddsApiError):       # 401/403: missing/invalid key or plan lacks access
    pass


class QuotaExceeded(OddsApiError):   # 429 after retries (rate limit or usage quota)
    pass


class BadRequest(OddsApiError):      # 422 etc.: e.g. an unknown market key — never retried
    pass


class NotFound(OddsApiError):        # 404: e.g. event no longer listed
    pass


@dataclass
class Usage:
    last: int | None
    used: int | None
    remaining: int | None


@dataclass
class ApiResponse:
    data: Any
    usage: Usage
    status: int
    path: str
    duration_ms: int


def _int_header(headers: httpx.Headers, name: str) -> int | None:
    v = headers.get(name)
    try:
        return int(float(v)) if v is not None else None
    except ValueError:
        return None


RequestHook = Callable[[str, int | None, Usage | None, int, str | None], None]


class OddsApiClient:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, on_request: RequestHook | None = None):
        self.settings = settings
        key = settings.odds_api_key.reveal()
        register_secret(key)
        self._key = key
        self._sleep = sleep
        self._on_request = on_request
        self._http = httpx.Client(base_url=settings.odds_api_base_url, timeout=settings.request_timeout_s,
                                  transport=transport, headers={"User-Agent": "nba-edge/0.1"})

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- public endpoints -----------------------------------------------------------
    def list_events(self, commence_from: str | None = None, commence_to: str | None = None) -> ApiResponse:
        params: dict[str, Any] = {"dateFormat": "iso"}
        if commence_from:
            params["commenceTimeFrom"] = commence_from
        if commence_to:
            params["commenceTimeTo"] = commence_to
        return self._get(f"/v4/sports/{self.settings.sport_key}/events", params)

    def event_odds(self, event_id: str, markets: tuple[str, ...] | list[str]) -> ApiResponse:
        params: dict[str, Any] = {
            "markets": ",".join(markets),
            "oddsFormat": "american",
            "dateFormat": "iso",
        }
        # When specific books are requested they take priority over regions in the
        # provider's docs; regions is still sent as a fallback scope.
        if self.settings.bookmakers:
            params["bookmakers"] = ",".join(self.settings.bookmakers)
        else:
            params["regions"] = self.settings.regions
        return self._get(f"/v4/sports/{self.settings.sport_key}/events/{event_id}/odds", params)

    # -- transport ------------------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any]) -> ApiResponse:
        if not self._key:
            raise MissingApiKey("ODDS_API_KEY is not set — refusing to call the odds provider.")
        attempt = 0
        while True:
            attempt += 1
            started = time.monotonic()
            status: int | None = None
            usage: Usage | None = None
            error: str | None = None
            try:
                resp = self._http.get(path, params={**params, "apiKey": self._key})
                status = resp.status_code
                usage = Usage(_int_header(resp.headers, "x-requests-last"), _int_header(resp.headers, "x-requests-used"),
                              _int_header(resp.headers, "x-requests-remaining"))
                if status == 200:
                    try:
                        data = resp.json()
                    except ValueError:
                        error = "HTTP 200 with a body that is not valid JSON"
                        raise OddsApiError(error) from None
                    return ApiResponse(data, usage, status, path, int((time.monotonic() - started) * 1000))
                error = self._error_text(resp)
                exc = self._classify(status, error)
            except httpx.TransportError as e:  # DNS, connect, read timeouts, resets
                error = f"network error: {type(e).__name__}: {e}"
                exc = OddsApiError(error, retryable=True)
            finally:
                if self._on_request:
                    try:
                        self._on_request(path, status, usage, int((time.monotonic() - started) * 1000), scrub(error) if error else None)
                    except Exception:  # recording must never break fetching
                        log.exception("request hook failed")
            if exc.retryable and attempt <= self.settings.max_retries:
                delay = min(30.0, 0.5 * 2 ** (attempt - 1))
                log.warning("odds API %s failed (%s); retry %d/%d in %.1fs", path, exc, attempt, self.settings.max_retries, delay)
                self._sleep(delay)
                continue
            raise exc

    @staticmethod
    def _error_text(resp: httpx.Response) -> str:
        try:
            body = resp.json()
            msg = body.get("message") if isinstance(body, dict) else None
            code = body.get("error_code") if isinstance(body, dict) else None
            text = f"{code}: {msg}" if code else (msg or resp.text[:300])
        except ValueError:
            text = resp.text[:300]
        return scrub(f"HTTP {resp.status_code}: {text}")

    @staticmethod
    def _classify(status: int, error: str) -> OddsApiError:
        if status in (401, 403):
            return AuthError(error, status=status)
        if status == 429:
            return QuotaExceeded(error, status=status, retryable=True)
        if status == 404:
            return NotFound(error, status=status)
        if status >= 500:
            return OddsApiError(error, status=status, retryable=True)
        return BadRequest(error, status=status)
