"""Engine creation + schema initialization."""
from __future__ import annotations

import os

from sqlalchemy import create_engine, event, insert, select
from sqlalchemy.engine import Engine

from .schema import SCHEMA_VERSION, metadata, schema_info, utcnow


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///"):]
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    engine = create_engine(url, future=True, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    return engine


def init_db(engine: Engine) -> int:
    """Create missing tables (idempotent) and record the schema version."""
    metadata.create_all(engine)
    with engine.begin() as conn:
        current = conn.execute(select(schema_info.c.version).order_by(schema_info.c.id.desc())).scalar()
        if current is None:
            conn.execute(insert(schema_info).values(version=SCHEMA_VERSION, applied_at=utcnow()))
            current = SCHEMA_VERSION
    return current
