"""Test fixtures. Every test uses a throwaway database:
  * SQLite in a temp dir — always;
  * Postgres — additionally, when NBA_EDGE_TEST_PG_URL points at a SCRATCH server
    (each test gets its own freshly created and dropped schema-less database).
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nba_edge.config import Secret, Settings  # noqa: E402
from nba_edge.db.engine import init_db, make_engine  # noqa: E402
from fake_provider import FakeOddsProvider, make_event  # noqa: E402

PG_URL = os.environ.get("NBA_EDGE_TEST_PG_URL")
BACKENDS = ["sqlite"] + (["postgres"] if PG_URL else [])


@pytest.fixture(params=BACKENDS)
def db_url(request, tmp_path):
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'test.sqlite3'}"
        return
    name = "nba_edge_test_" + uuid.uuid4().hex[:10]
    admin = create_engine(PG_URL.replace("postgresql://", "postgresql+psycopg://"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    base = PG_URL.rsplit("/", 1)[0]
    yield f"{base}/{name}"
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def engine(db_url):
    eng = make_engine(db_url)
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def settings(db_url):
    return Settings(database_url=Secret(db_url), odds_api_key=Secret("test-dummy-key-0000"),
                    odds_api_base_url="https://odds.invalid", markets=("player_points", "player_points_alternate", "player_rebounds"),
                    monthly_credit_budget=10_000, max_credits_per_run=5_000, max_retries=2)


NOW = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)  # 15:00 ET, Nov 7 2026


@pytest.fixture
def provider():
    """Two synthetic games tonight with synthetic FanDuel + Fanatics prices."""
    p = FakeOddsProvider()
    e1 = make_event("evt1", "Boston Celtics", "Miami Heat", NOW + timedelta(hours=4))       # 19:00 ET
    e2 = make_event("evt2", "Denver Nuggets", "Los Angeles Lakers", NOW + timedelta(hours=7))  # 22:00 ET
    p.add(e1, {
        "fanduel": {
            "player_points": [("Player A", "Over", 24.5, -115), ("Player A", "Under", 24.5, -105)],
            "player_points_alternate": [("Player A", "Over", 19.5, -400), ("Player A", "Over", 29.5, 260)],
            "player_rebounds": [("Player B", "Over", 8.5, 100), ("Player B", "Under", 8.5, -130)],
        },
        "fanatics": {"player_points": [("Player A", "Over", 24.5, -110), ("Player A", "Under", 24.5, -110)]},
    })
    p.add(e2, {"fanduel": {"player_points": [("Player C", "Over", 27.5, -120), ("Player C", "Under", 27.5, -102)]}})
    return p
