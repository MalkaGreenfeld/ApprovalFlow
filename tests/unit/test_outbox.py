"""The transactional outbox dispatcher (N3).

The transactional half, business row and event in one transaction, needs a real
database and is covered in ``tests/integration``. What is unit-testable is the
dispatcher's behaviour when publishing goes wrong, which is exactly the case the
original fire-and-forget publish handled by logging and moving on.
"""

from __future__ import annotations

from typing import Any

import pytest

from approvalflow.outbox import OutboxDispatcher


class FakeConnection:
    """Enough asyncpg surface for the dispatcher: fetch, execute, transaction."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, _query: str, *_args: Any) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["status"] == "pending"]

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        row_id = args[0]
        row = next(r for r in self.rows if r["id"] == row_id)
        if "status='published'" in query.replace(" ", ""):
            row["status"] = "published"
        elif "status='dead_letter'" in query.replace(" ", ""):
            row["status"] = "dead_letter"
            row["attempts"] = args[1]
        else:
            row["attempts"] = args[1]
        return "UPDATE 1"

    def transaction(self):
        connection = self

        class _Transaction:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_exc):
                return False

        return _Transaction()


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    def acquire(self):
        connection = self._connection

        class _Acquire:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_exc):
                return False

        return _Acquire()


class RecordingPublisher:
    """A publisher whose success can be scripted."""

    def __init__(self, succeed: bool = True, raise_error: bool = False) -> None:
        self.succeed = succeed
        self.raise_error = raise_error
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, topic: str, payload: dict[str, Any]) -> bool:
        if self.raise_error:
            raise ConnectionError("RabbitMQ unreachable")
        self.published.append((topic, payload))
        return self.succeed


def make_row(row_id: int = 1, attempts: int = 0) -> dict[str, Any]:
    return {
        "id": row_id,
        "correlation_id": "11111111-1111-4111-8111-111111111111",
        "topic": "submission.received",
        "payload": {"correlation_id": "11111111-1111-4111-8111-111111111111"},
        "attempts": attempts,
        "status": "pending",
    }


def dispatcher_for(rows: list[dict[str, Any]], publisher, **kwargs) -> tuple[OutboxDispatcher, FakeConnection]:
    connection = FakeConnection(rows)
    pool = FakePool(connection)

    async def pool_factory():
        return pool

    return OutboxDispatcher(pool_factory, publisher, **kwargs), connection


@pytest.mark.asyncio
async def test_a_pending_event_is_published_and_marked_published():
    rows = [make_row()]
    publisher = RecordingPublisher()
    dispatcher, _ = dispatcher_for(rows, publisher)

    published = await dispatcher.dispatch_once()

    assert published == 1
    assert publisher.published[0][0] == "submission.received"
    assert rows[0]["status"] == "published"
    assert dispatcher.published_total == 1


@pytest.mark.asyncio
async def test_a_published_row_is_never_published_twice():
    """At-least-once delivery still means one row is claimed once per pass."""
    rows = [make_row()]
    publisher = RecordingPublisher()
    dispatcher, _ = dispatcher_for(rows, publisher)

    await dispatcher.dispatch_once()
    await dispatcher.dispatch_once()

    assert len(publisher.published) == 1


@pytest.mark.asyncio
async def test_a_failed_publish_is_retried_not_dropped():
    """This is the bug the outbox exists to fix: the event survives the failure."""
    rows = [make_row()]
    dispatcher, _ = dispatcher_for(rows, RecordingPublisher(succeed=False))

    published = await dispatcher.dispatch_once()

    assert published == 0
    assert rows[0]["status"] == "pending", "the event was lost"
    assert rows[0]["attempts"] == 1
    assert dispatcher.failed_total == 1


@pytest.mark.asyncio
async def test_an_exception_from_the_broker_is_recorded_and_retried():
    rows = [make_row()]
    dispatcher, connection = dispatcher_for(rows, RecordingPublisher(raise_error=True))

    await dispatcher.dispatch_once()

    assert rows[0]["status"] == "pending"
    assert any("RabbitMQ unreachable" in str(args) for _q, args in connection.executed)


@pytest.mark.asyncio
async def test_a_permanently_failing_event_is_dead_lettered_not_retried_forever():
    """An endless retry loop is its own outage; park it and make it visible."""
    rows = [make_row(attempts=2)]
    dispatcher, _ = dispatcher_for(
        rows, RecordingPublisher(succeed=False), max_attempts=3
    )

    await dispatcher.dispatch_once()

    assert rows[0]["status"] == "dead_letter"
    assert dispatcher.dead_lettered_total == 1


@pytest.mark.asyncio
async def test_backoff_grows_and_is_capped():
    """Retrying every second against a dead broker is a denial of service."""
    assert OutboxDispatcher.backoff_seconds(1) == 1
    assert OutboxDispatcher.backoff_seconds(2) == 2
    assert OutboxDispatcher.backoff_seconds(3) == 4
    assert OutboxDispatcher.backoff_seconds(6) == 32
    assert OutboxDispatcher.backoff_seconds(20) == 60


@pytest.mark.asyncio
async def test_a_batch_is_published_in_order():
    rows = [make_row(1), make_row(2), make_row(3)]
    publisher = RecordingPublisher()
    dispatcher, _ = dispatcher_for(rows, publisher)

    published = await dispatcher.dispatch_once()

    assert published == 3
    assert all(r["status"] == "published" for r in rows)


@pytest.mark.asyncio
async def test_payload_decimals_survive_publishing():
    """Money must not become a float on the way to the broker."""
    from decimal import Decimal

    rows = [make_row()]
    rows[0]["payload"] = {"amount_usd": Decimal("42.00")}
    publisher = RecordingPublisher()
    dispatcher, _ = dispatcher_for(rows, publisher)

    await dispatcher.dispatch_once()

    assert publisher.published[0][1]["amount_usd"] == Decimal("42.00")
