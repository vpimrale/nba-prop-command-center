import logging

import httpx
import pytest

from nba_edge.config import Secret, Settings, redact_url
from nba_edge.logging_setup import RedactingFilter, configure_logging, register_secret, scrub
from nba_edge.odds.client import AuthError, BadRequest, MissingApiKey, OddsApiClient, OddsApiError, QuotaExceeded
from nba_edge.odds.markets import DEFAULT_MARKET_KEYS, market_info
from nba_edge.odds.math import american_to_decimal, decimal_to_american, implied_probability
from nba_edge.odds.normalize import normalize_event_odds, quote_key
from nba_edge.pipeline.budget import estimate_event_call_cost, estimate_month
from fake_provider import FakeOddsProvider, make_event, make_odds
from conftest import NOW


def test_odds_math():
    assert american_to_decimal(-110) == pytest.approx(1.909090909)
    assert american_to_decimal(150) == 2.5
    assert american_to_decimal(100) == 2.0 and american_to_decimal(-100) == 2.0
    assert decimal_to_american(2.5) == 150 and decimal_to_american(1.5) == -200
    assert implied_probability(-400) == pytest.approx(0.8)
    for bad in (0, 50, -99):
        with pytest.raises(ValueError):
            american_to_decimal(bad)


def test_market_catalogue():
    assert len(DEFAULT_MARKET_KEYS) == 22
    assert market_info("player_points_alternate").alternate is True
    assert market_info("player_points_rebounds_assists").code == "PRA"
    assert market_info("player_double_double").code == ""  # unknown markets are kept, not dropped


def test_normalize_flattens_quotes_and_skips_unusable_outcomes():
    ev = make_event("e", "Boston Celtics", "Miami Heat", NOW)
    payload = make_odds(ev, {"fanduel": {
        "player_points": [("Player A", "Over", 24.5, -115), ("Player A", "Under", 24.5, -105), ("", "Over", 1.5, -110)],
        "player_points_alternate": [("Player A", "Over", 29.5, 260)],
    }})
    payload["bookmakers"][0]["markets"][0]["outcomes"].append({"name": "Over", "description": "No Price"})
    payload["bookmakers"][0]["markets"][0]["outcomes"].append({"name": "Over", "description": "Player A", "price": -999, "point": 24.5})  # dup key
    r = normalize_event_odds(payload)
    assert len(r.quotes) == 3 and r.skipped == 3
    q = {x.key: x for x in r.quotes}
    k = quote_key("the-odds-api", "e", "fanduel", "player_points", "Player A", "over", 24.5)
    assert q[k].american_odds == -115 and q[k].decimal_odds == pytest.approx(1.869565, rel=1e-5)
    alt = [x for x in r.quotes if x.is_alternate][0]
    assert alt.market_code == "PTS" and alt.line == 29.5
    assert alt.game_date_et == "2026-11-07"


def test_budget_estimates_are_upper_bounds_from_config():
    s = Settings(bookmakers=("fanduel", "fanatics"))
    assert estimate_event_call_cost(s) == 22  # 22 markets x 1 region-unit (<=10 books)
    m = estimate_month(s, games_per_day=7.5, snapshots_per_game=4, days=30)
    assert m["estimated_credits_upper_bound"] == 19_800
    assert "20K" in m["smallest_published_tier_that_fits_UNVERIFIED"]


def _client(settings, provider, sleeps=None):
    return OddsApiClient(settings, transport=provider.transport(), sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def test_client_refuses_without_key(settings):
    p = FakeOddsProvider()
    c = OddsApiClient(Settings(), transport=p.transport())
    with pytest.raises(MissingApiKey):
        c.list_events()
    assert p.requests == []


def test_client_sends_documented_params_and_reads_usage_headers(settings, provider):
    c = _client(settings, provider)
    r = c.event_odds("evt1", settings.markets)
    req = provider.requests[-1]
    assert req["path"] == "/v4/sports/basketball_nba/events/evt1/odds"
    assert req["params"]["markets"] == "player_points,player_points_alternate,player_rebounds"
    assert req["params"]["bookmakers"] == "fanduel,fanatics" and req["params"]["oddsFormat"] == "american"
    assert r.usage.last == 3 and r.usage.remaining == provider.remaining


def test_client_retries_transient_errors_but_not_client_errors(settings, provider):
    sleeps = []
    path = "/v4/sports/basketball_nba/events/evt1/odds"
    provider.fail_next[path] = [500, 429]
    assert _client(settings, provider, sleeps).event_odds("evt1", settings.markets).status == 200
    assert sleeps == [0.5, 1.0]
    provider.fail_next[path] = [429, 429, 429]
    with pytest.raises(QuotaExceeded):
        _client(settings, provider).event_odds("evt1", settings.markets)
    n = len(provider.requests)
    provider.fail_next[path] = [422]
    with pytest.raises(BadRequest):
        _client(settings, provider).event_odds("evt1", settings.markets)
    assert len(provider.requests) == n + 1, "422 is not retried"
    provider.fail_next[path] = [401]
    with pytest.raises(AuthError):
        _client(settings, provider).event_odds("evt1", settings.markets)


def test_client_retries_network_errors(settings):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("boom", request=request)
    c = OddsApiClient(settings, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(OddsApiError) as ei:
        c.list_events()
    assert calls["n"] == settings.max_retries + 1 and ei.value.retryable


def test_secret_never_printed_or_logged(settings, provider, caplog):
    key = settings.odds_api_key.reveal()
    assert key not in repr(settings) and key not in str(settings.describe())
    assert repr(Secret("abc")) == "Secret(<set>)"
    assert redact_url("postgresql://u:pw@h/db") == "postgresql://u:***@h/db"
    register_secret(key)
    assert scrub(f"GET /x?apiKey={key}&a=1 key={key}") == "GET /x?apiKey=***&a=1 key=***"
    configure_logging("DEBUG")
    caplog.handler.addFilter(RedactingFilter())
    provider.fail_next["/v4/sports/basketball_nba/events"] = [500, 500, 500]
    logging.getLogger("httpx").setLevel(logging.DEBUG)  # even if someone turns httpx logging back on
    with pytest.raises(OddsApiError) as ei:
        _client(settings, provider).list_events()
    logging.getLogger("x").error("oops %s", f"https://h/v4?apiKey={key}")
    assert key not in caplog.text and key not in str(ei.value)
