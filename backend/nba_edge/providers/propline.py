"""Adapter: PropLine (https://prop-line.com) — FREE tier, verified live 2026-11-07.

Free tier (from PropLine's pricing page + response headers): 1,000 requests/UTC day,
no card; all books incl. FanDuel and Fanatics; current odds incl. player props and alt
ladders. Analytics endpoints (EV, history, trends, resolution) are paid and NOT used.
Terms (prop-line.com/terms, effective 2026-04-27): internal use allowed; bulk
redistribution prohibited — so raw responses must never be committed or published.

Endpoints used (all GET, auth header `X-API-Key`):
  /sports/{sport}/events                    upcoming events (+ espn_event_id)
  /sports/{sport}/odds?markets&bookmakers   BULK: near-term slate incl. player props (1 request)
  /sports/{sport}/events/{id}/odds?...      one event (fallback for events not in bulk)
Every response carries X-Daily-Limit / -Used / -Remaining / -Reset (UTC midnight).

Response shape (observed): bookmakers[].markets[] with `key`, `line_type`
(main | milestone | ...), `period`, `suspended_at`, `last_update`; outcomes with
`name` (Over/Under, or "10+ Points" on milestones), `description` (player), `point`,
`price` (American), `player_id` ("nba:<personId>"), `last_seen_at`, `last_change_at`.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable

import httpx

from ..config import Settings
from ..logging_setup import register_secret, scrub
from ..odds.client import (ApiResponse, AuthError, BadRequest, MissingApiKey, NotFound, OddsApiError, QuotaExceeded,
                           Usage)
from ..odds.markets import base_market_key, market_info
from ..odds.math import american_to_decimal
from ..odds.normalize import NormalizeResult, Quote, game_date_et, parse_ts
from .base import EventOdds, FetchOutcome, OddsProvider, ProviderEvent

log = logging.getLogger(__name__)
NAME = "propline"

# Daily-fantasy pick'em boards quote synthetic or payout-scaled prices, not sportsbook
# odds — never stored as if they were betting prices.
DFS_BOOKS = frozenset({"prizepicks", "underdog", "pick6", "sleeper", "dabble", "parlayplay", "dst_propbuilder", "betr_picks", "chalkboard"})
MILESTONE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*\+")
WITHDRAWN_GAP = timedelta(minutes=2)  # PropLine: an outcome whose last_seen_at lags its market's last_update is being pulled


class BurstLimited(OddsApiError):
    """Short-window rate limit (retryable after retry_after)."""


def _hdr_int(h: httpx.Headers, name: str) -> int | None:
    try:
        v = h.get(name)
        return int(float(v)) if v is not None else None
    except ValueError:
        return None


class PropLineClient:
    def __init__(self, settings: Settings, *, transport=None, sleep: Callable[[float], None] = time.sleep, on_request=None):
        self.settings = settings
        self._key = settings.propline_api_key.reveal()
        register_secret(self._key)
        self._sleep = sleep
        self._on_request = on_request
        self._http = httpx.Client(base_url=settings.propline_base_url.rstrip("/") + "/", timeout=settings.request_timeout_s,
                                  transport=transport, headers={"User-Agent": "nba-edge/0.2"})
        self.last_quota: dict | None = None

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, params: dict | None = None) -> ApiResponse:
        if not self._key:
            raise MissingApiKey("PROPLINE_API_KEY is not set — refusing to call PropLine.")
        attempt = 0
        while True:
            attempt += 1
            started = time.monotonic()
            status = usage = error = None
            try:
                resp = self._http.get(path.lstrip("/"), params=params or {}, headers={"X-API-Key": self._key})
                status = resp.status_code
                usage = Usage(1, _hdr_int(resp.headers, "X-Daily-Used"), _hdr_int(resp.headers, "X-Daily-Remaining"))
                if _hdr_int(resp.headers, "X-Daily-Limit") is not None:
                    self.last_quota = {"limit": _hdr_int(resp.headers, "X-Daily-Limit"), "used": usage.used,
                                       "remaining": usage.remaining, "reset_epoch": _hdr_int(resp.headers, "X-Daily-Reset")}
                if status == 200:
                    try:
                        data = resp.json()
                    except ValueError:
                        error = "HTTP 200 with a body that is not valid JSON"
                        raise OddsApiError(error) from None
                    return ApiResponse(data, usage, status, path, int((time.monotonic() - started) * 1000))
                exc, retry_after = self._classify(resp)
                error = str(exc)
            except httpx.TransportError as e:
                error = f"network error: {type(e).__name__}: {e}"
                exc, retry_after = OddsApiError(error, retryable=True), None
            finally:
                if self._on_request:
                    try:
                        self._on_request("/v1/" + path.lstrip("/"), status, usage, int((time.monotonic() - started) * 1000), scrub(error) if error else None)
                    except Exception:
                        log.exception("request hook failed")
            if exc.retryable and attempt <= self.settings.max_retries:
                delay = retry_after if retry_after is not None else min(30.0, 0.5 * 2 ** (attempt - 1))
                delay = min(float(delay), 60.0)
                log.warning("PropLine %s failed (%s); retry %d/%d in %.1fs", path, exc, attempt, self.settings.max_retries, delay)
                self._sleep(delay)
                continue
            raise exc

    @staticmethod
    def _classify(resp: httpx.Response) -> tuple[OddsApiError, float | None]:
        status = resp.status_code
        code = message = None
        retry_after = None
        try:
            body = resp.json()
            detail = body.get("detail") if isinstance(body, dict) else None
            if isinstance(detail, dict):
                code, message = detail.get("error"), detail.get("message")
                retry_after = detail.get("retry_after_seconds")
            elif detail:
                message = str(detail)
        except ValueError:
            message = resp.text[:200]
        if retry_after is None and resp.headers.get("Retry-After"):
            try:
                retry_after = float(resp.headers["Retry-After"])
            except ValueError:
                pass
        text = scrub(f"HTTP {status}: {code + ': ' if code else ''}{message or ''}".strip())
        if status == 401:
            return AuthError(text, status=status), None
        if status == 403:
            # e.g. upgrade_required: the endpoint/feature is not on the free tier — never retried
            return (AuthError if code in ("invalid_api_key", "missing_api_key") else BadRequest)(text, status=status), None
        if status == 429:
            if code == "burst_limit_exceeded":
                return BurstLimited(text, status=status, retryable=True), retry_after
            # daily cap (or unknown 429): stop — retrying cannot succeed before the UTC reset
            return QuotaExceeded(text, status=status), None
        if status == 404:
            return NotFound(text, status=status), None
        if status >= 500:
            return OddsApiError(text, status=status, retryable=True), retry_after
        return BadRequest(text, status=status), None


def normalize_propline_event(payload: dict, *, fetched_at: datetime, books: tuple[str, ...], stale_after: timedelta,
                             provider: str = NAME) -> NormalizeResult:
    event_id = str(payload["id"])
    commence = parse_ts(payload.get("commence_time"))
    base = dict(provider=provider, sport_key=payload.get("sport_key", ""), event_id=event_id, commence_time=commence,
                game_date_et=game_date_et(commence), home_team=payload.get("home_team", ""), away_team=payload.get("away_team", ""))
    wanted = set(books)
    quotes: list[Quote] = []
    seen_keys: set[str] = set()
    seen_books: set[str] = set()
    excluded: dict[str, int] = {}
    skipped = stale_n = 0

    def exclude(reason: str, n: int = 1) -> None:
        excluded[reason] = excluded.get(reason, 0) + n

    for book in payload.get("bookmakers") or []:
        bkey = book.get("key")
        if not bkey or (wanted and bkey not in wanted):
            continue
        if bkey in DFS_BOOKS:
            exclude("dfs_book", sum(len(m.get("outcomes") or []) for m in book.get("markets") or []))
            continue
        seen_books.add(bkey)
        book_ts = parse_ts(book.get("last_update"))
        book_stale = book_ts is None or (fetched_at - book_ts) > stale_after
        for market in book.get("markets") or []:
            mkey_raw = market.get("key")
            outcomes = market.get("outcomes") or []
            if not mkey_raw:
                skipped += len(outcomes)
                continue
            if market.get("period") not in (None, "", "game", "full_game"):
                exclude("non_full_game_period", len(outcomes))
                continue
            if market.get("suspended_at"):
                exclude("suspended_market", len(outcomes))
                continue
            lt = (market.get("line_type") or "main").lower()
            mkey = mkey_raw if lt == "main" else f"{base_market_key(mkey_raw)}_{'alternate' if lt == 'alternate' else lt}"
            info = market_info(mkey)
            m_ts = parse_ts(market.get("last_update"))
            for o in outcomes:
                player = (o.get("description") or "").strip()
                label = (o.get("name") or "").strip()
                price = o.get("price")
                if not player or not label or price is None:
                    skipped += 1
                    continue
                m = MILESTONE_RE.match(label)
                if lt == "milestone" or (m and o.get("point") is None):
                    if not m:
                        skipped += 1
                        continue
                    side, line = "over", float(m.group(1)) - 0.5   # "10+ Points" == Over 9.5
                else:
                    side = label.lower()
                    if side not in ("over", "under", "yes", "no"):
                        skipped += 1   # unrecognized outcome structure — never guessed
                        continue
                    try:
                        line = float(o["point"]) if o.get("point") is not None else None
                    except (TypeError, ValueError):
                        skipped += 1
                        continue
                seen = parse_ts(o.get("last_seen_at"))
                if m_ts and seen and (m_ts - seen) > WITHDRAWN_GAP:
                    exclude("withdrawn_outcome")
                    continue
                try:
                    american = int(round(float(price)))
                    decimal = round(american_to_decimal(american), 6)
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                q = Quote(**base, bookmaker=bkey, market_key=mkey, market_code=info.code, is_alternate=lt != "main",
                          player_name=player, side=side, line=line, american_odds=american, decimal_odds=decimal,
                          book_last_update=book_ts, market_last_update=m_ts, provider_player_id=o.get("player_id"),
                          line_type=lt, outcome_label=label, source_seen_at=seen, source_changed_at=parse_ts(o.get("last_change_at")),
                          stale=book_stale)
                if q.key in seen_keys:
                    skipped += 1
                    continue
                seen_keys.add(q.key)
                quotes.append(q)
                stale_n += int(book_stale)
    return NormalizeResult(quotes, skipped, seen_books, stale=stale_n, excluded=excluded)


class PropLineProvider(OddsProvider):
    name = NAME
    budget_window = "day"
    key_env_var = "PROPLINE_API_KEY"

    def __init__(self, settings: Settings, *, recorder=None, transport=None, sleep=None, clock=None):
        self.settings = settings
        kwargs: dict[str, Any] = {"transport": transport, "on_request": recorder}
        if sleep is not None:
            kwargs["sleep"] = sleep
        self.client = PropLineClient(settings, **kwargs)
        self._clock = clock

    # PropLine returns alt ladders under the same keys as main lines, so only base
    # market keys are requested (the-odds-api style `_alternate` keys are folded in).
    @property
    def markets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(base_market_key(m) for m in self.settings.markets))

    @property
    def requested_bookmakers(self) -> tuple[str, ...]:
        return tuple(b for b in self.settings.bookmakers if b not in DFS_BOOKS)

    def has_credentials(self) -> bool:
        return bool(self.settings.propline_api_key)

    def _now(self) -> datetime:
        from ..db.schema import utcnow
        return self._clock() if self._clock else utcnow()

    def list_events(self, start: datetime, end: datetime) -> list[ProviderEvent]:
        out = []
        for ev in self.client.get(f"/sports/{self.settings.sport_key}/events").data or []:
            commence = parse_ts(ev.get("commence_time"))
            if not ev.get("id") or not commence or not (start <= commence <= end):
                continue
            out.append(ProviderEvent(str(ev["id"]), ev.get("sport_key") or self.settings.sport_key, commence, ev.get("home_team", ""),
                                     ev.get("away_team", ""), str(ev["espn_event_id"]) if ev.get("espn_event_id") else None, bool(ev.get("live"))))
        return out

    def estimate_units(self, n_events: int) -> int:
        return 1 if n_events else 0   # one bulk request covers the near-term slate

    def _params(self) -> dict:
        p = {"markets": ",".join(self.markets)}
        if self.requested_bookmakers:
            p["bookmakers"] = ",".join(self.requested_bookmakers)
        return p

    def _event_odds(self, payload: dict) -> EventOdds:
        commence = parse_ts(payload.get("commence_time"))
        ev = ProviderEvent(str(payload["id"]), payload.get("sport_key") or self.settings.sport_key, commence, payload.get("home_team", ""),
                           payload.get("away_team", ""), str(payload["espn_event_id"]) if payload.get("espn_event_id") else None,
                           bool(payload.get("live")))
        res = normalize_propline_event(payload, fetched_at=self._now(), books=self.requested_bookmakers,
                                       stale_after=timedelta(minutes=self.settings.stale_after_min))
        return EventOdds(ev, res)

    def fetch_odds(self, event_ids: list[str], *, max_units: int) -> FetchOutcome:
        out = FetchOutcome()
        if not event_ids:
            return out
        if max_units < 1:
            out.not_fetched = list(event_ids)
            return out
        wanted = list(dict.fromkeys(event_ids))
        units = 0
        by_id: dict[str, dict] = {}
        try:
            bulk = self.client.get(f"/sports/{self.settings.sport_key}/odds", self._params())
            units += 1
            for p in bulk.data or []:
                if isinstance(p, dict) and p.get("id") is not None:
                    by_id[str(p["id"])] = p
        except (AuthError, QuotaExceeded, MissingApiKey):
            raise
        except OddsApiError as e:
            units += 1
            out.notes.append(f"bulk odds request failed ({e}); falling back to per-event requests")
        for ev_id in wanted:
            payload = by_id.get(ev_id)
            if payload is None:
                if units >= max_units:
                    out.not_fetched.append(ev_id)
                    continue
                try:
                    payload = self.client.get(f"/sports/{self.settings.sport_key}/events/{ev_id}/odds", self._params()).data
                    units += 1
                except NotFound as e:
                    units += 1
                    out.failed[ev_id] = f"not found ({e})"
                    continue
                except (AuthError, QuotaExceeded, MissingApiKey):
                    raise
                except OddsApiError as e:
                    units += 1
                    out.failed[ev_id] = str(e)
                    continue
            if not isinstance(payload, dict) or str(payload.get("id")) != ev_id:
                out.failed[ev_id] = "response was for a different event — ignored"
                continue
            out.odds.append(self._event_odds(payload))
        return out

    def market_in_scope(self, market_key: str) -> bool:
        return base_market_key(market_key) in self.markets

    def close(self) -> None:
        self.client.close()
