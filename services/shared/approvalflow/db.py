"""Shared PostgreSQL access: one asyncpg pool per service process.

The system uses both a relational store and the Dapr state store, for different
kinds of data:

* PostgreSQL holds the business records that need transactions, joins and
  reporting (submissions, the outbox, decisions, the approval queue). The audit
  trail, the dashboard and the ceiling proof are all queries over these.
* Dapr state holds runtime state that suits the building block better: the saga,
  budget counters with ETag concurrency, the HITL pause token and the live policy
  configuration.

The pool is created lazily and closed on shutdown. A service that cannot reach
its database fails its health check rather than starting up broken.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Collection
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg

from .logging import get_logger

logger = get_logger(__name__)

_pool: asyncpg.Pool | None = None
_lock = asyncio.Lock()


def dsn() -> str:
    """Build the connection string from the standard POSTGRES_* variables."""
    user = os.getenv("POSTGRES_USER", "approvalflow")
    password = os.getenv("POSTGRES_PASSWORD", "approvalflow")
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    database = os.getenv("POSTGRES_DB", "approvalflow")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


async def init_connection(conn: asyncpg.Connection) -> None:
    """Register JSON codecs so JSONB round-trips as plain Python dicts.

    With this registered, a jsonb parameter takes a Python object and asyncpg
    encodes it. Passing an already-``json.dumps``-ed string instead makes it
    encode twice and stores a JSON *string* where an object belongs.

    Public, and used by the integration fixtures too: a test pool built without
    it behaves differently from the production pool, which is how a
    double-encoding bug survived the test suite once already.
    """
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def get_pool() -> asyncpg.Pool:
    """Return the process-wide pool, creating it on first use.

    Raises:
        asyncpg.PostgresError: if the database is unreachable, deliberately not
            swallowed, so startup fails loudly rather than silently degrading.
    """
    global _pool
    if _pool is not None:
        return _pool
    async with _lock:
        if _pool is None:
            _pool = await asyncpg.create_pool(
                dsn(),
                min_size=int(os.getenv("DB_POOL_MIN", "2")),
                max_size=int(os.getenv("DB_POOL_MAX", "10")),
                command_timeout=float(os.getenv("DB_COMMAND_TIMEOUT", "15")),
                init=init_connection,
            )
            logger.info("PostgreSQL pool ready (max_size=%s)", os.getenv("DB_POOL_MAX", "10"))
    assert _pool is not None
    return _pool


async def wait_for_pool(attempts: int = 20, delay: float = 3.0) -> asyncpg.Pool:
    """Retry pool creation while PostgreSQL finishes booting.

    Compose healthchecks cover most of this, but a service restart during a
    database restart should recover on its own rather than crash-loop.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await get_pool()
        except Exception as exc:  # pragma: no cover - infrastructure timing
            last_error = exc
            logger.warning(
                "PostgreSQL not ready (attempt %s/%s): %s", attempt, attempts, exc
            )
            await asyncio.sleep(delay)
    raise RuntimeError(f"PostgreSQL unavailable after {attempts} attempts: {last_error}")


async def close_pool() -> None:
    """Close the pool on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("PostgreSQL pool closed")


async def healthy() -> bool:
    """Cheap liveness probe used by ``/health``."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception as exc:
        logger.error("Database health check failed: %s", exc)
        return False


def json_safe(value: Any) -> Any:
    """Recursively convert ``Decimal`` to ``str`` so values survive JSON."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def row_to_dict(
    row: Any, json_columns: Collection[str] = (), *, decode_all_json: bool = False
) -> dict[str, Any]:
    """Convert an ``asyncpg.Record`` into JSON-safe primitives.

    Lives here rather than in each service because all four repositories need the
    identical conversion, and the money rule in particular must not drift: a
    ``Decimal`` becomes a **string**, never a float, so an amount cannot lose
    precision on its way to the ceiling proof or the dashboard.

    Args:
        row: The record (or any mapping) to convert.
        json_columns: Columns stored as JSON/JSONB that arrive as text when no
            codec is registered on the connection, and should be decoded.
        decode_all_json: Try to decode every string column that looks like JSON.
            Off by default: guessing would corrupt a note that happens to start
            with a brace.

    Returns:
        A plain dict safe to hand to ``JSONResponse``.
    """
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, (Decimal, UUID)):
            out[key] = str(value)
        elif hasattr(value, "isoformat"):  # date / datetime
            out[key] = value.isoformat()
        elif isinstance(value, (list, tuple)):
            out[key] = [json_safe(v) for v in value]
        elif isinstance(value, str) and (
            key in json_columns or (decode_all_json and value[:1] in "[{")
        ):
            try:
                out[key] = json.loads(value)
            except json.JSONDecodeError:
                out[key] = value
        else:
            out[key] = value
    return out
