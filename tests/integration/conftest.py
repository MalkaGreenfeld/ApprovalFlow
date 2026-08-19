"""Integration-test fixtures: a real PostgreSQL with the real schema.

Each test gets a clean schema applied from db/init/001-schema.sql, the same file
Compose runs, so the tests exercise the shipped DDL and not a hand-rolled copy
that can drift from it.

These fixtures drop and recreate schemas, so they must never point at a database
a running system is using. Two guards enforce that: they connect to a separate
database (approvalflow_test by default, created on demand) rather than
POSTGRES_DB, and they refuse to run unless its name ends with _test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from approvalflow.db import init_connection

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = REPO_ROOT / "db" / "init" / "001-schema.sql"

#: Schemas owned by the application, dropped between tests.
APP_SCHEMAS = ("notification", "payment", "router", "ingestion")


def _connection_settings() -> dict[str, str]:
    return {
        "user": os.getenv("POSTGRES_USER", "approvalflow"),
        "password": os.getenv("POSTGRES_PASSWORD", "approvalflow"),
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": os.getenv("POSTGRES_PORT", "5432"),
    }


def test_database_name() -> str:
    """The dedicated test database. Never ``POSTGRES_DB``."""
    return os.getenv("POSTGRES_TEST_DB", "approvalflow_test")


def dsn(database: str | None = None) -> str:
    settings = _connection_settings()
    name = database or test_database_name()
    return (
        f"postgresql://{settings['user']}:{settings['password']}"
        f"@{settings['host']}:{settings['port']}/{name}"
    )


async def _ensure_test_database(asyncpg) -> None:
    """Create the test database if it does not exist yet.

    Connects to the ``postgres`` maintenance database to do it, so no assumption
    is made about what already exists.
    """
    name = test_database_name()
    admin = await asyncpg.connect(dsn("postgres"), timeout=5)
    try:
        exists = await admin.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", name
        )
        if not exists:
            # CREATE DATABASE cannot run inside a transaction; the identifier is
            # validated by the _test suffix guard below.
            await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()


@pytest.fixture
async def pool():
    """A connection pool against a freshly created schema, dropped afterwards."""
    asyncpg = pytest.importorskip("asyncpg")

    name = test_database_name()
    if not name.endswith("_test"):
        pytest.fail(
            f"refusing to run destructive integration tests against '{name}': "
            "the database name must end with '_test'"
        )

    try:
        await _ensure_test_database(asyncpg)
        connection = await asyncpg.connect(dsn(), timeout=5)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL not reachable ({exc})")

    schema_sql = SCHEMA.read_text(encoding="utf-8")
    await _drop_schemas(connection)
    await connection.execute(schema_sql)
    await connection.close()

    # Same connection initialisation as the production pool. A test pool without
    # the JSON codecs encodes jsonb differently, which is how a double-encoding
    # bug passed this suite once already.
    created = await asyncpg.create_pool(
        dsn(), min_size=1, max_size=4, init=init_connection
    )
    try:
        yield created
    finally:
        await created.close()
        cleanup = await asyncpg.connect(dsn())
        await _drop_schemas(cleanup)
        await cleanup.close()


async def _drop_schemas(connection) -> None:
    for schema in APP_SCHEMAS:
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
