"""PropLine provider: normalization, client error handling, bulk/fallback fetching, and the
snapshot pipeline end to end — all against SYNTHETIC data (tests/fake_propline.py)."""
import json
import logging
import os
import subprocess
import sys
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, insert, select, text

from nba_edge.config import Secret, Settings
from nba_edge.db.engine import init_db
from nba_edge.db.schema import api_requests, games, odds_current, odds_snapshots
from nba_edge.odds.client import AuthError, BadRequest, OddsApiError, QuotaExceeded
from nba_edge.odds.normalize import quote_key
from nba_edge.pipeline.schedule import fixed_slot_times, run_due
from nba_edge.pipeline.snapshots import open_provider, refresh_events, run_snapshot, upcoming_event_ids
from nba_edge.providers.propline import DFS_BOOKS, PropLineClient, normalize_propline_event
from nba_edge.queries import coverage_report, status_report, verify_report
from conftest import NOW
from fake_propline import iso, ladder, market, ou, standard_slate

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "proxy-injected"


@pytest.fixture
def pl_settings(db_url):
    return Settings(database_url=Secret(db_url), odds_provider="propline", propline_api_key=Secret(KEY),
                    propline_base_url="https://api.invalid/v1", bookmakers=("fanduel", "fanatics", "prizepicks"),
                    markets=("player_points", "player_points_alternate", "player_rebounds", "player_assists"),
                    daily_request_budget=800, max_credits_per_run=50, max_retries=2, stale_after_min=20)


@pytest.fixture
def fake():
    return standard_slate(NOW)


def _open(engine, s, fake, now=NOW, sleeps=None):
    return open_provider(s, engine, transport=fake.transport(), sleep=(sleeps.append if sleeps is not None else (lambda x: None)),
                         clock=lambda: now)


def _snap(engine, s, fake, now=NOW, ids=None, sleeps=None):
    p, rec = _open(engine, s, fake, now, sleeps)
    with p:
        if ids is None:
            refresh_events(engine, p, now)
            ids = upcoming_event_ids(engine, now, provider=p.name)
        return run_snapshot(engine, s, p, rec, event_ids=ids, now=now)


def _rows(engine):
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(select(odds_snapshots).order_by(odds_snapshots.c.id))]


# ---------------------------------------------------------------- normalization
def _event(books, now=NOW):
    return {"id": "9", "sport_key": "basketball_nba", "commence_time": iso(now + timedelta(hours=3)), "home_team": "H", "away_team": "A",
            "bookmakers": [{"key": k, "title": k, "last_update": iso(v.get("updated", now)), "markets": v["markets"]} for k, v in books.items()]}


def test_normalize_main_milestone_alternate_and_provenance():
    s = NOW - timedelta(minutes=1)
    ev = _event({"fanduel": {"markets": [
        market("player_points", "P One", ou("P One", 24.5, -110, -118, s, "nba:1"), updated=s),
        market("player_points", "P One", ladder("P One", [(25, -105), (30, 250)], "Points", s, "nba:1"), line_type="milestone", updated=s),
        market("player_threes", "P One", ou("P One", 3.5, 140, -180, s, "nba:1"), line_type="alternate", updated=s),
    ]}})
    r = normalize_propline_event(ev, fetched_at=NOW, books=("fanduel",), stale_after=timedelta(minutes=20))
    q = {x.key: x for x in r.quotes}
    main = q[quote_key("propline", "9", "fanduel", "player_points", "P One", "over", 24.5)]
    assert main.is_alternate is False and main.line_type == "main" and main.provider_player_id == "nba:1" and main.market_code == "PTS"
    rung = q[quote_key("propline", "9", "fanduel", "player_points_milestone", "P One", "over", 24.5)]
    assert rung.american_odds == -105 and rung.is_alternate and rung.outcome_label == "25+ Points" and rung.market_code == "PTS"
    assert rung.key != main.key, "a milestone and the O/U main line on the same number stay distinct quotes"
    alt = q[quote_key("propline", "9", "fanduel", "player_threes_alternate", "P One", "under", 3.5)]
    assert alt.is_alternate and alt.market_code == "3PM"
    assert len(r.quotes) == 6 and r.skipped == 0 and r.stale == 0


def test_normalize_excludes_suspended_withdrawn_dfs_periods_and_bad_rows():
    s = NOW - timedelta(minutes=1)
    withdrawn = ou("P Two", 9.5, -110, -110, s - timedelta(minutes=10))
    bad = [{"name": "Over", "description": "P Three", "point": 5.5, "price": 50}, {"name": "Over", "description": "", "point": 5.5, "price": -110},
           {"name": "Over", "description": "P Three", "point": 5.5}, {"name": "", "description": "P Three", "point": 5.5, "price": -110},
           {"name": "Ten Points", "description": "P Three", "point": None, "price": 200}]
    ev = _event({
        "fanduel": {"markets": [
            market("player_points", "P One", ou("P One", 24.5, -110, -110, s), updated=s, suspended=True),
            market("player_rebounds", "P Two", withdrawn, updated=s),
            market("player_points", "P One", ou("P One", 12.5, -110, -110, s), updated=s, period="1h"),
            market("player_assists", "P Three", bad, updated=s),
            market("player_assists", "P Three", bad[4:], line_type="milestone", updated=s),
        ]},
        "prizepicks": {"markets": [market("player_points", "P One", ou("P One", 24.5, 100, 100, s), updated=s)]},
    })
    r = normalize_propline_event(ev, fetched_at=NOW, books=("fanduel", "prizepicks"), stale_after=timedelta(minutes=20))
    assert r.quotes == []
    assert r.excluded == {"suspended_market": 2, "withdrawn_outcome": 2, "non_full_game_period": 2, "dfs_book": 2}
    assert r.skipped == 6
    assert "prizepicks" in DFS_BOOKS and r.bookmakers_seen == {"fanduel"}


def test_normalize_flags_stale_books_but_keeps_their_prices_marked():
    old = NOW - timedelta(minutes=45)
    ev = _event({"fanduel": {"updated": old, "markets": [market("player_points", "P", ou("P", 20.5, -110, -110, old), updated=old)]},
                 "fanatics": {"markets": [market("player_points", "P", ou("P", 20.5, -110, -110, NOW), updated=NOW)]}})
    ev["bookmakers"].append({"key": "draftkings", "title": "DK", "last_update": None,
                             "markets": [market("player_points", "P", ou("P", 20.5, -110, -110, NOW), updated=NOW)]})
    r = normalize_propline_event(ev, fetched_at=NOW, books=("fanduel", "fanatics", "draftkings"), stale_after=timedelta(minutes=20))
    by_book = {}
    for q in r.quotes:
        by_book.setdefault(q.bookmaker, set()).add(q.stale)
    assert by_book == {"fanduel": {True}, "fanatics": {False}, "draftkings": {True}}, "unknown freshness counts as stale"
    assert r.stale == 4


# ---------------------------------------------------------------- client
def test_client_sends_key_header_and_reads_daily_quota(pl_settings, fake):
    c = PropLineClient(pl_settings, transport=fake.transport())
    r = c.get("/sports/basketball_nba/events")
    assert fake.requests[-1]["has_key"] and "apiKey" not in fake.requests[-1]["params"], "key goes in the X-API-Key header, never the URL"
    assert r.usage.last == 1 and r.usage.used == 1 and r.usage.remaining == 999
    assert c.last_quota["limit"] == 1000


def test_client_burst_limit_waits_retry_after_then_succeeds(pl_settings, fake):
    sleeps = []
    fake.fail_next["/v1/sports/basketball_nba/events"] = [(429, {"error": "burst_limit_exceeded", "message": "slow down", "retry_after_seconds": 3})]
    c = PropLineClient(pl_settings, transport=fake.transport(), sleep=sleeps.append)
    assert c.get("/sports/basketball_nba/events").status == 200
    assert sleeps == [3.0]


def test_client_daily_limit_and_gated_features_are_not_retried(pl_settings, fake):
    path = "/v1/sports/basketball_nba/events"
    for status, detail, exc in [(429, {"error": "daily_limit_exceeded", "message": "cap"}, QuotaExceeded),
                                (403, {"error": "upgrade_required", "message": "Hobby+", "required_tier": "hobby"}, BadRequest),
                                (401, {"error": "invalid_api_key", "message": "bad key"}, AuthError)]:
        n = len(fake.requests)
        fake.fail_next[path] = [(status, detail)]
        with pytest.raises(exc) as ei:
            PropLineClient(pl_settings, transport=fake.transport(), sleep=lambda s: None).get("/sports/basketball_nba/events")
        assert len(fake.requests) == n + 1, f"{status} must not be retried"
        assert detail["error"] in str(ei.value)


def test_client_retries_5xx_and_network_errors_then_gives_up(pl_settings, fake):
    path = "/v1/sports/basketball_nba/events"
    fake.fail_next[path] = [(502, {"error": "bad_gateway"}), (503, {"error": "unavailable"})]
    assert PropLineClient(pl_settings, transport=fake.transport(), sleep=lambda s: None).get("/sports/basketball_nba/events").status == 200
    calls = {"n": 0}

    def boom(req):
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out", request=req)
    with pytest.raises(OddsApiError) as ei:
        PropLineClient(pl_settings, transport=httpx.MockTransport(boom), sleep=lambda s: None).get("/x")
    assert calls["n"] == pl_settings.max_retries + 1 and ei.value.retryable
    bad_json = httpx.MockTransport(lambda req: httpx.Response(200, text="<html>"))
    with pytest.raises(OddsApiError, match="not valid JSON"):
        PropLineClient(pl_settings, transport=bad_json).get("/x")


def test_client_never_logs_or_raises_with_the_key(pl_settings, fake, caplog):
    secret = "pl-secret-value-123456"
    s = Settings(database_url=pl_settings.database_url, odds_provider="propline", propline_api_key=Secret(secret),
                 propline_base_url="https://api.invalid/v1", max_retries=1)
    fake.fail_next["/v1/sports/basketball_nba/events"] = [(500, {"error": "x", "message": f"echo {secret}"}), (500, {"error": "x"})]
    from nba_edge.logging_setup import RedactingFilter
    caplog.handler.addFilter(RedactingFilter())
    caplog.set_level(logging.DEBUG)
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    with pytest.raises(OddsApiError) as ei:
        PropLineClient(s, transport=fake.transport(), sleep=lambda x: None).get("/sports/basketball_nba/events")
    assert secret not in str(ei.value) and secret not in caplog.text


# ---------------------------------------------------------------- provider fetch
def test_bulk_covers_tonight_and_per_event_fallback_covers_the_rest(engine, pl_settings, fake):
    p, _ = _open(engine, pl_settings, fake)
    out = p.fetch_odds(["1001", "1002", "1003"], max_units=10)
    paths = [r["path"] for r in fake.requests]
    assert paths == ["/v1/sports/basketball_nba/odds", "/v1/sports/basketball_nba/events/1003/odds"]
    assert sorted(e.event.event_id for e in out.odds) == ["1001", "1002", "1003"]
    assert fake.requests[0]["params"]["bookmakers"] == "fanduel,fanatics", "DFS books are never requested"
    assert fake.requests[0]["params"]["markets"] == "player_points,player_rebounds,player_assists"


def test_budget_exhausted_mid_fetch_leaves_events_unfetched_not_failed(engine, pl_settings, fake):
    p, _ = _open(engine, pl_settings, fake)
    out = p.fetch_odds(["1001", "1003"], max_units=1)
    assert [e.event.event_id for e in out.odds] == ["1001"] and out.not_fetched == ["1003"]


def test_bulk_failure_falls_back_and_404_is_reported(engine, pl_settings, fake):
    fake.fail_next["/v1/sports/basketball_nba/odds"] = [(500, {"error": "x"})] * 3
    p, _ = _open(engine, pl_settings, fake)
    out = p.fetch_odds(["1001", "9999"], max_units=10)
    assert [e.event.event_id for e in out.odds] == ["1001"]
    assert "9999" in out.failed and "not found" in out.failed["9999"]
    assert any("bulk odds request failed" in n for n in out.notes)


# ---------------------------------------------------------------- pipeline end to end
def test_snapshot_stores_fanduel_and_fanatics_with_provenance(engine, pl_settings, fake):
    r = _snap(engine, pl_settings, fake)
    assert r["status"] == "ok" and r["provider"] == "propline" and r["events_fetched"] == 3
    assert r["credits_reported"] == 2 and r["credits_remaining"] == 997  # bulk + 1 fallback; events list is a separate job
    rows = _rows(engine)
    assert {x["bookmaker"] for x in rows} == {"fanduel", "fanatics"}
    assert all(x["provider"] == "propline" and x["stale"] is False for x in rows)
    rung = next(x for x in rows if x["market_key"] == "player_points_milestone" and x["line"] == 24.5)
    assert rung["outcome_label"] == "25+ Points" and rung["provider_player_id"] == "nba:1" and rung["is_alternate"]
    assert "dfs_book" not in (r["detail"] or ""), "DFS books are filtered at request time"
    with engine.connect() as c:
        espn = c.execute(select(games.c.espn_event_id).where(games.c.provider_event_id == "1001")).scalar()
    assert espn == "401000001", "provider's ESPN event id is kept for joining to the tracker"


def test_rung_removal_is_tombstoned_but_other_books_and_unrequested_markets_are_not(engine, pl_settings, fake):
    _snap(engine, pl_settings, fake)
    fd = fake.book("1001", "fanduel")
    mil = next(m for m in fd["markets"] if m["line_type"] == "milestone")
    mil["outcomes"] = [o for o in mil["outcomes"] if o["name"] != "30+ Points"]
    narrow = Settings(**{**pl_settings.__dict__, "markets": ("player_points",)})
    r = _snap(engine, narrow, fake, now=NOW + timedelta(minutes=10), ids=["1001"])
    assert r["rows_removed"] == 1
    gone = [x for x in _rows(engine) if x["change_type"] == "removed"]
    assert gone[0]["market_key"] == "player_points_milestone" and gone[0]["line"] == 29.5
    with engine.connect() as c:
        reb = c.execute(select(odds_current.c.market_status).where(odds_current.c.market_key == "player_rebounds")).scalars().all()
    assert reb == ["active", "active"], "player_rebounds was not requested this run, so it is not tombstoned"


def test_daily_quota_exhausted_stops_the_run_without_tombstones(engine, pl_settings, fake):
    _snap(engine, pl_settings, fake)
    fake.used = fake.daily_limit  # provider says the day's cap is spent
    r = _snap(engine, pl_settings, fake, now=NOW + timedelta(minutes=10), ids=["1001", "1002"])
    assert r["status"] == "quota_exhausted" and r["rows_removed"] == 0
    assert "daily_limit_exceeded" in r["detail"]


def test_stale_feed_is_stored_flagged_and_reported(engine, pl_settings, fake):
    fd = fake.book("1002", "fanduel")
    fd["last_update"] = iso(NOW - timedelta(hours=2))
    r = _snap(engine, pl_settings, fake, ids=["1002"])
    assert r["quotes_stale"] == 2 and "flagged stale" in r["detail"]
    st = status_report(engine, NOW)
    assert st["active_pregame_quotes_flagged_stale"] == 2
    cov = {(x["bookmaker"], x["market_key"]): x for x in coverage_report(engine, "2026-11-07")}
    assert cov[("fanduel", "player_assists")]["stale_rows"] == 2 and cov[("fanatics", "player_assists")]["stale_rows"] == 0


def test_live_or_started_games_are_never_stored(engine, pl_settings, fake):
    fake.events["1002"]["live"] = True
    r = _snap(engine, pl_settings, fake, ids=["1001", "1002"])
    assert all(x["provider_event_id"] == "1001" for x in _rows(engine))
    assert "1002: already started" in r["detail"]


def test_missing_bookmaker_and_missing_key_are_reported(engine, pl_settings, fake):
    for ev in ("1001", "1002"):
        fake.events[ev]["bookmakers"] = [b for b in fake.events[ev]["bookmakers"] if b["key"] != "fanatics"]
    r = _snap(engine, pl_settings, fake, ids=["1001", "1002"])
    assert "fanatics" in r["detail"] and r["bookmakers_seen"] == "fanduel"
    nokey = Settings(**{**pl_settings.__dict__, "propline_api_key": Secret(None)})
    n = len(fake.requests)
    r2 = _snap(engine, nokey, fake, ids=["1001"])
    assert r2["status"] == "blocked_no_key" and "PROPLINE_API_KEY" in r2["detail"] and len(fake.requests) == n


def test_daily_budget_guard_uses_todays_usage_only(engine, pl_settings, fake):
    with engine.begin() as c:  # 800 requests already spent today; plenty yesterday
        c.execute(insert(api_requests), [{"requested_at": NOW - timedelta(minutes=5), "provider": "propline", "path": "/v1/x", "credits_last": 800},
                                         {"requested_at": NOW - timedelta(days=1), "provider": "propline", "path": "/v1/x", "credits_last": 900,
                                          "credits_remaining": 0}])
    n = len(fake.requests)
    r = _snap(engine, pl_settings, fake, ids=["1001"])
    assert r["status"] == "skipped_budget" and len(fake.requests) == n
    roomy = Settings(**{**pl_settings.__dict__, "daily_request_budget": 1000})
    r2 = _snap(engine, roomy, fake, ids=["1001"])
    assert r2["status"] == "ok", "yesterday's 'remaining: 0' must not block today"


def test_verify_report_checks_pairs_overround_and_cross_book_lines(engine, pl_settings, fake):
    _snap(engine, pl_settings, fake)
    v = verify_report(engine, "2026-11-07")
    fd, fa = v["books"]["fanduel"], v["books"]["fanatics"]
    assert fd["main_lines_two_sided"] == 3 and fd["alternate_quotes"] == 3 and fd["invalid_prices"] == 0
    assert fa["main_lines_two_sided"] == 2 and fa["with_provider_player_id"] == 4
    assert 1.0 < fd["overround_main_min_median_max"][1] < 1.2
    assert v["cross_book_main_line"] == {"player_markets_on_all_books": 2, "identical_line": 2, "identical_pct": 100.0}


def test_interval_slots_and_scheduler_tick_with_propline(engine, pl_settings, fake):
    s = Settings(**{**pl_settings.__dict__, "snapshot_interval_min": 30, "interval_start_et": "14:00", "interval_end_et": "16:00"})
    assert fixed_slot_times(s) == ["10:00", "14:00", "14:30", "15:00", "15:30", "16:00"]
    t1 = run_due(engine, s, now=NOW + timedelta(minutes=2), transport=fake.transport(), sleep=lambda x: None)  # 15:02 ET
    assert t1["status"] == "ok" and t1["provider"] == "propline" and [r["reason"] for r in t1["runs"]] == ["fixed"]
    n = len(fake.requests)
    t2 = run_due(engine, s, now=NOW + timedelta(minutes=4), transport=fake.transport(), sleep=lambda x: None)
    assert t2["runs"] == [] and len(fake.requests) == n


def test_schema_migration_adds_missing_columns(engine):
    with engine.begin() as c:
        c.execute(text("ALTER TABLE odds_snapshots DROP COLUMN stale"))
    init_db(engine)
    with engine.connect() as c:
        c.execute(select(odds_snapshots.c.stale)).all()
        assert c.execute(select(func.count()).select_from(odds_snapshots)).scalar() == 0


def test_cli_propline_end_to_end_over_real_http(tmp_path):
    from datetime import datetime, timezone
    real_now = datetime.now(timezone.utc)
    fake = standard_slate(real_now)
    server, base = fake.serve()
    try:
        env = {**os.environ, "NBA_EDGE_DATABASE_URL": f"sqlite:///{tmp_path / 'pl.sqlite3'}", "ODDS_PROVIDER": "propline",
               "PROPLINE_API_KEY": "cli-placeholder-key-987", "PROPLINE_BASE_URL": base, "PYTHONPATH": BACKEND}
        run = lambda *a, e=env: subprocess.run([sys.executable, "-m", "nba_edge", *a], cwd=BACKEND, env=e, capture_output=True, text=True, timeout=60)
        r = run("snapshot")
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert out["status"] == "ok" and out["provider"] == "propline" and out["rows_inserted"] > 0
        v = run("verify")
        assert v.returncode == 0 and "fanduel" in json.loads(v.stdout)["books"]
        for res in (r, v, run("status"), run("config")):
            assert "cli-placeholder-key-987" not in res.stdout + res.stderr
        blocked = run("snapshot", e={k: val for k, val in env.items() if k != "PROPLINE_API_KEY"})
        assert blocked.returncode == 2 and "PROPLINE_API_KEY" in blocked.stderr
    finally:
        server.shutdown()


def test_daily_request_estimate_fits_free_tier():
    from nba_edge.pipeline.budget import estimate_day_requests
    s = Settings(odds_provider="propline", snapshot_interval_min=15)
    e = estimate_day_requests(s, games_per_day=15)
    assert e["fixed_and_interval_runs"] == 53 and e["pretip_runs_upper_bound"] == 30
    assert e["requests_per_day_upper_bound"] == 107 and e["fits_daily_budget"]
