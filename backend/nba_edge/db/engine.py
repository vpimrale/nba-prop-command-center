"""Engine creation + schema initialization."""
from __future__ import annotations

import os

from sqlalchemy import create_engine, event, inspect, insert, select, text
from sqlalchemy.engine import Engine

from . import research_schema  # noqa: F401  (registers research/model tables on metadata)
from .paper_schema import install_immutability_triggers
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


def _add_missing_columns(engine: Engine) -> list[str]:
    """Additive migration: add any nullable column defined in the schema but missing from
    an existing table (never drops or rewrites anything)."""
    added = []
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                if not col.nullable:
                    raise RuntimeError(f"cannot auto-add NOT NULL column {table.name}.{col.name}")
                ddl = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}'))
                added.append(f"{table.name}.{col.name}")
    return added


def init_db(engine: Engine) -> int:
    """Create missing tables, add missing nullable columns (idempotent) and record the
    schema version."""
    metadata.create_all(engine)
    _add_missing_columns(engine)
    install_immutability_triggers(engine)
    with engine.begin() as conn:
        current = conn.execute(select(schema_info.c.version).order_by(schema_info.c.id.desc())).scalar()
        if current is None or current < SCHEMA_VERSION:
            conn.execute(insert(schema_info).values(version=SCHEMA_VERSION, applied_at=utcnow()))
            current = SCHEMA_VERSION
    return current
