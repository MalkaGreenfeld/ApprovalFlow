"""The transactional guarantees, against a real PostgreSQL (N3 / N6 / M10).

Three things can only honestly be tested with a database:

1. the business row and its event really do share one transaction;
2. duplicate detection holds when two identical submissions arrive together;
3. the decision row's unique constraint makes a redelivered event a no-op.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from uuid import uuid4

import pytest

from approvalflow.outbox import enqueue
from services.ingestion.app import repository as ingestion_repo

pytestmark = pytest.mark.asyncio


def submission_row(correlation_id: str, **overrides):
    row = {
        "correlation_id": correlation_id,
        "submitter_email": "dana.cohen@northwind.example",
        "department": "engineering-2026Q2",
        "vendor": "Bistro 19",
        "vendor_known": True,
        "invoice_number": "NW-INV-7781",
        "currency": "USD",
        "amount_original": Decimal("42.00"),
        "amount_usd": Decimal("42.00"),
        "category": "meals",
        "attendees": 1,
        "line_items": [{"description": "Team lunch", "quantity": 1, "unitPrice": "38.89"}],
        "tax_amount": Decimal("3.11"),
        "total": Decimal("42.00"),
        "receipt_present": True,
        "math_ok": True,
        "raw_payload": {"vendor": "Bistro 19"},
        "notes": "Team lunch",
        "status": "received",
        "idempotency_key": "bistro 19:nw-inv-7781:42.00",
        "duplicate_of": None,
    }
    row.update(overrides)
    return row


async def test_the_submission_and_its_event_share_one_transaction(pool):
    """Both land, or neither does."""
    correlation_id = str(uuid4())

    async with pool.acquire() as conn, conn.transaction():
        await ingestion_repo.insert_submission(conn, submission_row(correlation_id))
        await enqueue(
            conn,
            correlation_id=correlation_id,
            topic="submission.received",
            payload={"correlation_id": correlation_id, "amount_usd": Decimal("42.00")},
        )

    async with pool.acquire() as conn:
        submissions = await conn.fetchval(
            "SELECT COUNT(*) FROM ingestion.submissions WHERE correlation_id = $1::uuid",
            correlation_id,
        )
        outbox = await conn.fetchrow(
            "SELECT topic, status, payload FROM ingestion.outbox WHERE correlation_id = $1::uuid",
            correlation_id,
        )

    assert submissions == 1
    assert outbox["topic"] == "submission.received"
    assert outbox["status"] == "pending"
    payload = outbox["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    # Decimals are stringified rather than turned into floats.
    assert payload["amount_usd"] == "42.00"


async def test_a_failed_business_insert_publishes_nothing(pool):
    """The mirror case: a rolled-back submission leaves no event behind."""
    correlation_id = str(uuid4())

    # A named error rather than a blind `Exception`, so the test cannot pass on
    # some unrelated failure: anything going wrong after both writes must take
    # both of them down together.
    class SimulatedCrash(RuntimeError):
        """Stands in for a process dying between the two writes."""

    with pytest.raises(SimulatedCrash):
        async with pool.acquire() as conn:
            async with conn.transaction():
                await ingestion_repo.insert_submission(conn, submission_row(correlation_id))
                await enqueue(
                    conn,
                    correlation_id=correlation_id,
                    topic="submission.received",
                    payload={"correlation_id": correlation_id},
                )
                raise SimulatedCrash("simulated failure after both writes")

    async with pool.acquire() as conn:
        submissions = await conn.fetchval(
            "SELECT COUNT(*) FROM ingestion.submissions WHERE correlation_id = $1::uuid",
            correlation_id,
        )
        events = await conn.fetchval(
            "SELECT COUNT(*) FROM ingestion.outbox WHERE correlation_id = $1::uuid",
            correlation_id,
        )

    assert submissions == 0
    assert events == 0, "an event survived a rolled-back submission"


async def test_the_dispatcher_publishes_pending_rows_and_marks_them(pool):
    from approvalflow.outbox import OutboxDispatcher

    correlation_id = str(uuid4())
    async with pool.acquire() as conn, conn.transaction():
        await ingestion_repo.insert_submission(conn, submission_row(correlation_id))
        await enqueue(
            conn,
            correlation_id=correlation_id,
            topic="submission.received",
            payload={"correlation_id": correlation_id},
        )

    published: list[str] = []

    class Publisher:
        async def publish(self, topic, _payload):
            published.append(topic)
            return True

    async def pool_factory():
        return pool

    dispatcher = OutboxDispatcher(pool_factory, Publisher())
    assert await dispatcher.dispatch_once() == 1
    assert published == ["submission.received"]
    # A second pass finds nothing: exactly one publish per row.
    assert await dispatcher.dispatch_once() == 0

    stats = await dispatcher.stats()
    assert stats["depth_by_status"].get("published") == 1


async def test_duplicate_detection_holds_for_simultaneous_submissions(pool):
    """Two identical invoices arriving at once: exactly one is treated as original.

    The advisory lock makes this deterministic. Without it both transactions read
    "no prior submission", both look original, and a duplicate reaches the payment
    path.
    """
    key = "bistro 19:nw-inv-7781:42.00"
    first_id, second_id = str(uuid4()), str(uuid4())

    async def submit(correlation_id: str) -> str | None:
        async with pool.acquire() as conn, conn.transaction():
            await ingestion_repo.lock_business_key(conn, key)
            prior = await ingestion_repo.find_prior_submission(
                conn, "Bistro 19", "NW-INV-7781", Decimal("42.00")
            )
            duplicate_of = str(prior["correlation_id"]) if prior else None
            await ingestion_repo.insert_submission(
                conn,
                submission_row(correlation_id, duplicate_of=duplicate_of),
            )
            return duplicate_of

    results = await asyncio.gather(submit(first_id), submit(second_id))

    originals = [r for r in results if r is None]
    duplicates = [r for r in results if r is not None]
    assert len(originals) == 1, f"expected exactly one original, got {results}"
    assert len(duplicates) == 1


async def test_a_terminal_rejection_may_legitimately_be_resubmitted(pool):
    """A rejected invoice is not "in flight", so a corrected re-submission is fine."""
    first_id = str(uuid4())
    async with pool.acquire() as conn, conn.transaction():
        await ingestion_repo.insert_submission(
            conn, submission_row(first_id, status="rejected")
        )

    async with pool.acquire() as conn:
        prior = await ingestion_repo.find_prior_submission(
            conn, "Bistro 19", "NW-INV-7781", Decimal("42.00")
        )

    assert prior is None


async def test_status_transitions_are_recorded_and_terminal_states_stick(pool):
    correlation_id = str(uuid4())
    async with pool.acquire() as conn, conn.transaction():
        await ingestion_repo.insert_submission(conn, submission_row(correlation_id))

    changed, previous = await ingestion_repo.update_status(
        pool, correlation_id, "auto_approved", event_type="decision.auto_approved"
    )
    assert changed and previous == "received"

    changed, _ = await ingestion_repo.update_status(
        pool, correlation_id, "paid", event_type="payment.completed"
    )
    assert changed

    # A redelivered escalation must not move a paid item back.
    changed, previous = await ingestion_repo.update_status(
        pool, correlation_id, "human_review", event_type="decision.escalated"
    )
    assert not changed
    assert previous == "paid"

    timeline = await ingestion_repo.timeline(pool, correlation_id)
    assert [entry["to_status"] for entry in timeline] == ["auto_approved", "paid"]


async def test_a_repeated_idempotency_key_replays_the_original_response(pool):
    """M10: a client retry must not create a second submission."""
    correlation_id = str(uuid4())
    body = {"correlation_id": correlation_id, "status": "received"}

    async with pool.acquire() as conn, conn.transaction():
        await ingestion_repo.insert_submission(conn, submission_row(correlation_id))
        await ingestion_repo.remember_response(conn, "client-key-1", correlation_id, body)

    async with pool.acquire() as conn:
        replayed = await ingestion_repo.replay_response(conn, "client-key-1")
        missing = await ingestion_repo.replay_response(conn, "client-key-2")

    assert replayed == body
    assert missing is None


async def test_a_decision_round_can_only_be_recorded_once(pool):
    """The router's redelivery guard is a database constraint, not a race-prone check."""
    correlation_id = str(uuid4())
    async with pool.acquire() as conn, conn.transaction():
        await ingestion_repo.insert_submission(conn, submission_row(correlation_id))

    insert = """
        INSERT INTO router.decisions (
            correlation_id, revision, enforced_amount_usd, enforced_category,
            ceiling_applied_usd, confidence_threshold, policy_config_version,
            final_route, decided_by
        ) VALUES ($1::uuid, 0, 42.00, 'meals', 750, 0.85, 1, 'auto_approve', 'router')
        ON CONFLICT (correlation_id, revision, decided_by) DO NOTHING
        RETURNING id
    """
    async with pool.acquire() as conn:
        first = await conn.fetchval(insert, correlation_id)
        second = await conn.fetchval(insert, correlation_id)

    assert first is not None
    assert second is None, "a redelivered decision event created a second row"


async def test_the_ceiling_proof_query_finds_a_planted_violation(pool):
    """A proof that never fails proves nothing, plant one and watch it be caught."""
    from services.router.app import repository as router_repo

    correlation_id = str(uuid4())
    async with pool.acquire() as conn:
        async with conn.transaction():
            await ingestion_repo.insert_submission(conn, submission_row(correlation_id))
        await conn.execute(
            """
            INSERT INTO router.decisions (
                correlation_id, revision, enforced_amount_usd, enforced_category,
                ceiling_applied_usd, confidence_threshold, agent_confidence,
                policy_config_version, final_route, decided_by
            ) VALUES ($1::uuid, 0, 5000.00, 'meals', 750, 0.85, 0.99, 1,
                      'auto_approve', 'router')
            """,
            correlation_id,
        )

    proof = await router_repo.ceiling_proof(pool)

    assert proof["holds"] is False
    assert proof["ceiling_violations"] == 1
    assert proof["offending_items"][0]["correlation_id"] == correlation_id


async def test_the_ceiling_proof_holds_for_compliant_decisions(pool):
    from services.router.app import repository as router_repo

    correlation_id = str(uuid4())
    async with pool.acquire() as conn:
        async with conn.transaction():
            await ingestion_repo.insert_submission(conn, submission_row(correlation_id))
        await conn.execute(
            """
            INSERT INTO router.decisions (
                correlation_id, revision, enforced_amount_usd, enforced_category,
                ceiling_applied_usd, confidence_threshold, agent_confidence,
                policy_config_version, final_route, decided_by
            ) VALUES ($1::uuid, 0, 42.00, 'meals', 750, 0.85, 0.95, 1,
                      'auto_approve', 'router')
            """,
            correlation_id,
        )

    proof = await router_repo.ceiling_proof(pool)

    assert proof["holds"] is True
    assert proof["auto_approvals_examined"] == 1
    assert proof["ceiling_violations"] == 0


async def test_the_dashboard_reports_the_autonomy_split(pool):
    from services.router.app import repository as router_repo

    async with pool.acquire() as conn:
        for route, amount in (
            ("auto_approve", "42.00"),
            ("auto_approve", "99.00"),
            ("human_review", "1400.00"),
            ("human_approved", "1400.00"),
        ):
            correlation_id = str(uuid4())
            async with conn.transaction():
                await ingestion_repo.insert_submission(
                    conn, submission_row(correlation_id, invoice_number=f"INV-{uuid4().hex[:6]}")
                )
            await conn.execute(
                """
                INSERT INTO router.decisions (
                    correlation_id, revision, enforced_amount_usd, enforced_category,
                    ceiling_applied_usd, confidence_threshold, agent_confidence,
                    policy_config_version, final_route, decided_by
                ) VALUES ($1::uuid, 0, $2, 'meals', 750, 0.85, 0.9, 1, $3, $4)
                """,
                correlation_id,
                Decimal(amount),
                route,
                "router" if route.startswith(("auto", "human_review")) else "manager@x.example",
            )

    dashboard = await router_repo.dashboard(pool, window_hours=24)

    assert dashboard["routes"]["auto_approve"] == 2
    assert dashboard["routes"]["human_review"] == 1
    assert dashboard["rates"]["auto_approval_rate"] == pytest.approx(2 / 3, abs=0.01)
    assert Decimal(dashboard["money_usd"]["auto_approved"]) == Decimal("141.00")
    assert Decimal(dashboard["money_usd"]["human_approved"]) == Decimal("1400.00")
