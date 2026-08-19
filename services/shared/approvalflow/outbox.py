"""Transactional outbox (N3).

A business row and the event it causes are written in one PostgreSQL
transaction::

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("INSERT INTO ingestion.submissions ...")
        await enqueue(conn, topic=..., payload=...)

Either both land or neither does, so there is no stored submission that nothing
was told about, and no event for a submission that does not exist.

OutboxDispatcher then publishes pending rows through Dapr with retry and
exponential backoff. Delivery is at-least-once, which the idempotent consumers
downstream already assume.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import UTC, datetime
from typing import Any, Protocol

import asyncpg

from .db import json_safe
from .logging import get_logger, set_correlation_id

logger = get_logger(__name__)

#: Give up after this many attempts and park the row for a human (dead letter).
MAX_ATTEMPTS = int(os.getenv("OUTBOX_MAX_ATTEMPTS", "8"))
#: How often the dispatcher looks for pending rows.
POLL_INTERVAL_SECONDS = float(os.getenv("OUTBOX_POLL_INTERVAL", "1.0"))
#: How many rows one pass claims, a simple bulkhead on publish concurrency.
BATCH_SIZE = int(os.getenv("OUTBOX_BATCH_SIZE", "25"))


class EventPublisher(Protocol):
    """Anything that can publish an event. Injected so tests need no sidecar."""

    async def publish(self, topic: str, payload: dict[str, Any]) -> bool: ...


async def enqueue(
    conn: asyncpg.Connection,
    *,
    correlation_id: str,
    topic: str,
    payload: dict[str, Any],
    table: str = "ingestion.outbox",
) -> int:
    """Append an event to the outbox **inside the caller's transaction**.

    Args:
        conn: Connection already inside a transaction.
        correlation_id: Correlation id carried on the event.
        topic: Dapr pub/sub topic.
        payload: Event body (``Decimal`` values are stringified).
        table: Qualified outbox table name.

    Returns:
        The outbox row id.
    """
    return int(
        await conn.fetchval(
            f"""
            INSERT INTO {table} (correlation_id, topic, payload)
            VALUES ($1::uuid, $2, $3::jsonb)
            RETURNING id
            """,
            correlation_id,
            topic,
            json_safe(payload),
        )
    )


class OutboxDispatcher:
    """Polls the outbox and publishes pending events.

    ``FOR UPDATE SKIP LOCKED`` means several replicas can run the dispatcher
    concurrently without publishing the same row twice, so this scales
    horizontally with the service.
    """

    def __init__(
        self,
        pool_factory: Any,
        publisher: EventPublisher,
        *,
        table: str = "ingestion.outbox",
        poll_interval: float = POLL_INTERVAL_SECONDS,
        batch_size: int = BATCH_SIZE,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self._pool_factory = pool_factory
        self._publisher = publisher
        self._table = table
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        #: Counters exposed on /health so a stuck outbox is visible.
        self.published_total = 0
        self.failed_total = 0
        self.dead_lettered_total = 0

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="outbox-dispatcher")
            logger.info("Outbox dispatcher started (interval=%ss)", self._poll_interval)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            # Shutdown must not raise: an unpublished row is picked up by the
            # next process to start.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
            logger.info("Outbox dispatcher stopped")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                dispatched = await self.dispatch_once()
                # Drain a backlog quickly, idle politely when empty.
                if dispatched == 0:
                    await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                raise
            except Exception as exc:  # pragma: no cover - keep the loop alive
                logger.exception("Outbox dispatch pass failed: %s", exc)
                await asyncio.sleep(self._poll_interval)

    # -- one pass --------------------------------------------------------

    async def dispatch_once(self) -> int:
        """Claim and publish one batch. Returns the number of rows published."""
        pool = await self._pool_factory()
        published = 0

        async with pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                f"""
                    SELECT id, correlation_id, topic, payload, attempts
                    FROM {self._table}
                    WHERE status = 'pending' AND next_attempt_at <= NOW()
                    ORDER BY id
                    LIMIT $1
                    FOR UPDATE SKIP LOCKED
                    """,
                self._batch_size,
            )

            for row in rows:
                correlation_id = str(row["correlation_id"])
                set_correlation_id(correlation_id)
                payload = row["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)

                ok = False
                error: str | None = None
                try:
                    ok = await self._publisher.publish(row["topic"], payload)
                except Exception as exc:
                    error = str(exc)
                    logger.error(
                        "Outbox publish raised for topic %s: %s", row["topic"], exc
                    )

                if ok:
                    await conn.execute(
                        f"UPDATE {self._table} SET status='published', "
                        f"published_at=NOW(), attempts=attempts+1, last_error=NULL "
                        f"WHERE id=$1",
                        row["id"],
                    )
                    published += 1
                    self.published_total += 1
                    logger.info(
                        "Outbox published %s (row %s)", row["topic"], row["id"]
                    )
                else:
                    attempts = int(row["attempts"]) + 1
                    self.failed_total += 1
                    if attempts >= self._max_attempts:
                        self.dead_lettered_total += 1
                        await conn.execute(
                            f"UPDATE {self._table} SET status='dead_letter', "
                            f"attempts=$2, last_error=$3 WHERE id=$1",
                            row["id"],
                            attempts,
                            error or "publish returned false",
                        )
                        logger.error(
                            "Outbox row %s dead-lettered after %s attempts "
                            "(topic=%s), requires operator attention",
                            row["id"],
                            attempts,
                            row["topic"],
                        )
                    else:
                        backoff = self.backoff_seconds(attempts)
                        await conn.execute(
                            f"UPDATE {self._table} SET attempts=$2, last_error=$3, "
                            f"next_attempt_at = NOW() + ($4 || ' seconds')::interval "
                            f"WHERE id=$1",
                            row["id"],
                            attempts,
                            error or "publish returned false",
                            str(backoff),
                        )
                        logger.warning(
                            "Outbox row %s retry %s/%s in %ss",
                            row["id"],
                            attempts,
                            self._max_attempts,
                            backoff,
                        )
        return published

    @staticmethod
    def backoff_seconds(attempts: int) -> int:
        """Exponential backoff capped at a minute: 1, 2, 4, 8, 16, 32, 60..."""
        return min(60, 2 ** max(0, attempts - 1))

    # -- introspection ---------------------------------------------------

    async def stats(self) -> dict[str, Any]:
        """Outbox depth by status, surfaced on ``/health`` and the dashboard."""
        pool = await self._pool_factory()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT status, COUNT(*) AS n FROM {self._table} GROUP BY status"
            )
        depth = {str(r["status"]): int(r["n"]) for r in rows}
        return {
            "depth_by_status": depth,
            "published_total": self.published_total,
            "failed_total": self.failed_total,
            "dead_lettered_total": self.dead_lettered_total,
            "checked_at": datetime.now(UTC).isoformat(),
        }


class DaprEventPublisher:
    """Publishes through the Dapr pub/sub building block (M5 async path)."""

    def __init__(self, pubsub_name: str = "pubsub", timeout: float = 10.0) -> None:
        self._pubsub = pubsub_name
        self._timeout = timeout
        self._port = os.getenv("DAPR_HTTP_PORT", "3500")

    def url(self, topic: str) -> str:
        return f"http://localhost:{self._port}/v1.0/publish/{self._pubsub}/{topic}"

    async def publish(self, topic: str, payload: dict[str, Any]) -> bool:
        import httpx

        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout)) as client:
            resp = await client.post(self.url(topic), json=json_safe(payload))
            if resp.status_code not in (200, 204):
                logger.error(
                    "Dapr publish to %s returned %s: %s",
                    topic,
                    resp.status_code,
                    resp.text[:200],
                )
                return False
            return True
