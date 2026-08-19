"""The shared connection pool, against a real PostgreSQL.

Carried over from dev. Skips rather than fails when no database is reachable, so
the suite behaves the same on a laptop as it does in CI.
"""

from __future__ import annotations

import asyncpg
import pytest

from approvalflow.db import close_pool, dsn, get_pool

pytestmark = pytest.mark.asyncio


async def _reachable() -> bool:
    try:
        conn = await asyncpg.connect(dsn(), timeout=5)
    except Exception:
        return False
    await conn.close()
    return True


async def test_the_pool_is_created_once_and_can_be_closed_and_reopened():
    if not await _reachable():
        pytest.skip(f"PostgreSQL not reachable at {dsn().rsplit('@', 1)[-1]}")

    await close_pool()
    try:
        pool = await get_pool()
        assert isinstance(pool, asyncpg.Pool)
        # The same pool is handed back rather than a second one being opened.
        assert await get_pool() is pool

        await close_pool()

        reopened = await get_pool()
        assert isinstance(reopened, asyncpg.Pool)
        assert reopened is not pool
    finally:
        await close_pool()


async def test_the_pool_runs_a_query():
    if not await _reachable():
        pytest.skip("PostgreSQL not reachable")

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT 1") == 1
    finally:
        await close_pool()
