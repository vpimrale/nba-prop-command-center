from datetime import timedelta

from sqlalchemy import func, select

from nba_edge.config import Secret, Settings
from nba_edge.db.schema import api_requests, odds_current, odds_snapshots, snapshot_runs
from nba_edge.odds.normalize import quote_key
from nba_edge.pipeline.snapshots import make_client, refresh_events, run_snapshot, upcoming_event_ids
from nba_edge.queries import closing_lines, coverage_report, price_at, status_report
from conftest import NOW

KEY_A_OVER = quote_key("the-odds-api", "evt1", "fanduel", "player_points", "Player A", "over", 24.5)


def _run(engine, settings, provider, now=NOW, event_ids=None, **kw):
    client, rec = make_client(settings, engine, transport=provider.transport(), sleep=lambda s: None, clock=lambda: now)
    with client:
        if event_ids is None:
            refresh_events(engine, client, now)
            event_ids = upcoming_event_ids(engine, now, provider=client.name)
        return run_snapshot(engine, settings, client, rec, event_ids=event_ids, now=now, **kw)


def _rows(engine):
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(select(odds_snapshots).order_by(odds_snapshots.c.id))]


def test_first_run_stores_every_quote_and_records_real_credit_usage(engine, settings, provider):
    r = _run(engine, settings, provider)
    assert r["status"] == "ok" and r["events_fetched"] == 2
    rows = _rows(engine)
    assert len(rows) == r["rows_inserted"] == r["quotes_seen"] == 10
    assert {x["change_type"] for x in rows} == {"new"}
    assert {x["bookmaker"] for x in rows} == {"fanduel", "fanatics"}
    a = next(x for x in rows if x["quote_key"] == KEY_A_OVER)
    assert a["american_odds"] == -115 and a["game_date_et"] == "2026-11-07" and a["is_alternate"] is False
    assert a["captured_at"] == NOW and a["model_projection"] is None
    assert r["credits_reported"] == provider.used == 4  # provider-reported, not estimated
    with engine.connect() as c:
        paths = [p for (p,) in c.execute(select(api_requests.c.path))]
    assert all("apiKey" not in p and "?" not in p for p in paths)


def test_unchanged_prices_add_no_rows_and_history_is_append_only(engine, settings, provider):
    _run(engine, settings, provider)
    before = _rows(engine)
    r2 = _run(engine, settings, provider, now=NOW + timedelta(minutes=30))
    assert r2["status"] == "ok" and r2["rows_inserted"] == 0 and r2["rows_removed"] == 0
    assert _rows(engine) == before
    provider.set_price("evt1", "fanduel", "player_points", "Player A", "Over", 24.5, -130)
    r3 = _run(engine, settings, provider, now=NOW + timedelta(minutes=60))
    after = _rows(engine)
    assert after[: len(before)] == before, "earlier rows are never modified"
    new = after[len(before):]
    assert len(new) == r3["rows_inserted"] == 1
    assert new[0]["change_type"] == "price_change" and new[0]["american_odds"] == -130 and new[0]["prev_american_odds"] == -115
    with engine.connect() as c:
        cur = c.execute(select(odds_current).where(odds_current.c.quote_key == KEY_A_OVER)).mappings().one()
    assert cur["american_odds"] == -130 and cur["last_changed_at"] == NOW + timedelta(minutes=60)


def test_disappearing_and_reappearing_lines_are_tombstoned_then_restored(engine, settings, provider):
    _run(engine, settings, provider)
    gone = provider.remove_outcome("evt1", "fanduel", "player_points_alternate", "Player A", "Over", 29.5)
    r = _run(engine, settings, provider, now=NOW + timedelta(minutes=5))
    assert r["rows_removed"] == 1
    alt_key = quote_key("the-odds-api", "evt1", "fanduel", "player_points_alternate", "Player A", "over", 29.5)
    assert price_at(engine, alt_key, NOW + timedelta(minutes=1))["american_odds"] == 260
    assert price_at(engine, alt_key, NOW + timedelta(minutes=6))["market_status"] == "removed"
    for b in provider.odds["evt1"]["bookmakers"]:
        for m in b["markets"]:
            if b["key"] == "fanduel" and m["key"] == "player_points_alternate":
                m["outcomes"].append(gone)
    r2 = _run(engine, settings, provider, now=NOW + timedelta(minutes=10))
    assert r2["rows_inserted"] == 1
    assert _rows(engine)[-1]["change_type"] == "reappeared"


def test_failed_fetch_never_writes_tombstones(engine, settings, provider):
    _run(engine, settings, provider)
    n = len(_rows(engine))
    provider.fail_next["/v4/sports/basketball_nba/events/evt1/odds"] = [500, 500, 500]
    r = _run(engine, settings, provider, now=NOW + timedelta(minutes=5), event_ids=["evt1", "evt2"])
    assert r["status"] == "partial" and r["events_failed"] == 1 and r["rows_removed"] == 0
    assert len(_rows(engine)) == n
    with engine.connect() as c:
        assert c.execute(select(func.count()).select_from(odds_current).where(odds_current.c.market_status == "removed")).scalar() == 0


def test_unrequested_markets_are_never_tombstoned(engine, settings, provider):
    _run(engine, settings, provider)
    narrow = Settings(database_url=settings.database_url, odds_provider="the-odds-api", bookmakers=settings.bookmakers, odds_api_key=settings.odds_api_key, markets=("player_points",), max_retries=0)
    r = _run(engine, narrow, provider, now=NOW + timedelta(minutes=5), event_ids=["evt1"])
    assert r["rows_removed"] == 0


def test_tipped_off_games_are_not_fetched(engine, settings, provider):
    after_first_tip = NOW + timedelta(hours=4, minutes=1)
    r = _run(engine, settings, provider, now=after_first_tip)
    assert r["events_planned"] == 1 and r["events_fetched"] == 1
    assert all(x["provider_event_id"] == "evt2" for x in _rows(engine))


def test_provider_saying_game_started_means_no_prices_stored(engine, settings, provider):
    # Not yet in our games table, and the provider reports a tip-off already in the past.
    provider.odds["evt1"]["commence_time"] = "2026-11-07T19:30:00Z"  # 30 min before NOW
    r = _run(engine, settings, provider, event_ids=["evt1"])
    assert r["rows_inserted"] == 0 and "already started" in r["detail"]
    assert _rows(engine) == []


def test_malformed_200_is_an_explicit_failure(engine, settings):
    import httpx
    from nba_edge.odds.client import OddsApiClient, OddsApiError
    c = OddsApiClient(settings, transport=httpx.MockTransport(lambda req: httpx.Response(200, text="<html>")))
    try:
        c.list_events()
        raise AssertionError("expected OddsApiError")
    except OddsApiError as e:
        assert "not valid JSON" in str(e)


def test_no_key_means_no_calls_and_an_explicit_blocked_run(engine, settings, provider):
    nokey = Settings(database_url=settings.database_url, odds_provider="the-odds-api", bookmakers=settings.bookmakers, odds_api_key=Secret(None))
    r = _run(engine, nokey, provider, event_ids=["evt1"])
    assert r["status"] == "blocked_no_key" and provider.requests == []
    assert "ODDS_API_KEY" in r["detail"]


def test_bad_key_is_reported_as_blocked_auth(engine, settings, provider):
    provider.fail_next["/v4/sports/basketball_nba/events/evt1/odds"] = [401]
    r = _run(engine, settings, provider, event_ids=["evt1"])
    assert r["status"] == "blocked_auth" and r["rows_inserted"] == 0


def test_budget_guard_limits_or_skips_calls(engine, settings, provider):
    tight = Settings(database_url=settings.database_url, odds_provider="the-odds-api", bookmakers=settings.bookmakers, odds_api_key=settings.odds_api_key, markets=settings.markets,
                     monthly_credit_budget=10_000, max_credits_per_run=3)  # 3 markets/event -> 1 event fits
    _run(engine, tight, provider, event_ids=[])  # no-op run (no_events)
    r = _run(engine, tight, provider, event_ids=["evt1", "evt2"])
    assert r["events_fetched"] == 1 and "budget" in r["detail"]
    zero = Settings(database_url=settings.database_url, odds_provider="the-odds-api", bookmakers=settings.bookmakers, odds_api_key=settings.odds_api_key, markets=settings.markets, max_credits_per_run=2)
    n = len(provider.requests)
    r2 = _run(engine, zero, provider, event_ids=["evt1"])
    assert r2["status"] == "skipped_budget" and len(provider.requests) == n


def test_missing_bookmaker_is_reported_not_hidden(engine, settings, provider):
    provider.odds["evt2"]["bookmakers"] = [b for b in provider.odds["evt2"]["bookmakers"] if b["key"] != "fanatics"]
    r = _run(engine, settings, provider, event_ids=["evt2"])
    assert r["status"] == "ok" and "fanatics" in (r["detail"] or "")
    assert r["bookmakers_seen"] == "fanduel"


def test_status_coverage_and_closing_queries(engine, settings, provider):
    _run(engine, settings, provider)
    provider.set_price("evt1", "fanatics", "player_points", "Player A", "Over", 24.5, -125)
    _run(engine, settings, provider, now=NOW + timedelta(hours=3, minutes=50))  # 10 min before evt1 tip
    provider.set_price("evt1", "fanatics", "player_points", "Player A", "Over", 24.5, -140)
    _run(engine, settings, provider, now=NOW + timedelta(hours=5), event_ids=["evt1"])  # after tip: not fetched
    close = {r["quote_key"]: r for r in closing_lines(engine, "evt1")}
    k = quote_key("the-odds-api", "evt1", "fanatics", "player_points", "Player A", "over", 24.5)
    assert close[k]["american_odds"] == -125 and close[k]["offered_at_close"] and close[k]["minutes_before_tip"] == 10.0
    cov = {(r["bookmaker"], r["market_key"]): r for r in coverage_report(engine, "2026-11-07")}
    assert cov[("fanduel", "player_points")]["games"] == 2 and cov[("fanatics", "player_points")]["games"] == 1
    st = status_report(engine, NOW + timedelta(hours=3, minutes=55))
    assert st["last_successful_snapshot"]["age_min"] == 5.0
    assert st["credits_used_this_month_reported"] == provider.used
    assert st["active_pregame_quotes_by_bookmaker"]["fanatics"] == 2
    with engine.connect() as c:
        statuses = [s for (s,) in c.execute(select(snapshot_runs.c.status).order_by(snapshot_runs.c.id))]
    assert statuses[-1] == "no_events"
