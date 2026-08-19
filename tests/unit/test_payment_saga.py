"""The payment saga: compensation (M9), idempotency (M10) and no overspend.

These exercise the real saga against an in-memory state store with genuine ETag
behaviour, so the compare-and-set loop under test is the one that runs in
production. The previous tests only asserted that the functions existed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from approvalflow.dapr_client import ConcurrentUpdateError
from services.payment.app import main as payment


@pytest.fixture
def saga_env(monkeypatch, fake_state):
    """Wire the payment module to the fake state store and capture its effects."""
    published: list[tuple[str, dict[str, Any]]] = []
    audit: list[dict[str, Any]] = []

    class FakePublisher:
        async def publish(self, topic: str, payload: dict[str, Any]) -> bool:
            published.append((topic, payload))
            return True

    async def fake_record_attempt(_pool, **kwargs):
        audit.append(kwargs)

    monkeypatch.setattr(payment, "state", fake_state)
    monkeypatch.setattr(payment, "publisher", FakePublisher())
    monkeypatch.setattr(payment.repo, "record_attempt", fake_record_attempt)
    monkeypatch.setattr(payment, "get_pool", _no_pool)
    monkeypatch.setattr(payment, "FAIL_INVOICE_NUMBERS", {"RS-90021"})
    return {"state": fake_state, "published": published, "audit": audit}


async def _no_pool():
    """The audit writer is stubbed, so no pool is needed."""
    return None


def event(**overrides: Any) -> dict[str, Any]:
    body = {
        "correlation_id": "11111111-1111-4111-8111-111111111111",
        "amount_usd": "600.00",
        "department": "marketing-2026Q2",
        "invoice_number": "EW-7001",
        "submitter_email": "marketing.lead@northwind.example",
    }
    body.update(overrides)
    return body


async def seed_budget(state, department: str, amount: str) -> None:
    await state.save(f"budget:{department}", {"department": department, "remaining": amount})


# ── Happy path ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_successful_saga_reserves_confirms_and_publishes_completion(saga_env):
    state = saga_env["state"]
    await seed_budget(state, "marketing-2026Q2", "1000.00")

    result = await payment.execute_saga(event())

    assert result["status"] == "SUCCESS"
    budget = await state.get("budget:marketing-2026Q2")
    assert Decimal(budget["remaining"]) == Decimal("400.00")
    assert [topic for topic, _ in saga_env["published"]] == ["payment.completed"]

    saga = await state.get("saga:11111111-1111-4111-8111-111111111111")
    assert saga["saga_state"] == "confirmed"
    reservation = await state.get(f"reservation:{saga['saga_id']}")
    assert reservation["status"] == "committed"


@pytest.mark.asyncio
async def test_money_arithmetic_is_exact(saga_env):
    """Repeated decimal amounts must not drift the budget by fractions of a cent."""
    state = saga_env["state"]
    await seed_budget(state, "marketing-2026Q2", "100.00")

    await payment.execute_saga(event(amount_usd="33.33"))

    budget = await state.get("budget:marketing-2026Q2")
    assert Decimal(budget["remaining"]) == Decimal("66.67")


# ── Idempotency (M10) ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_redelivered_approval_does_not_pay_twice(saga_env):
    state = saga_env["state"]
    await seed_budget(state, "marketing-2026Q2", "1000.00")

    first = await payment.execute_saga(event())
    second = await payment.execute_saga(event())

    assert first["status"] == "SUCCESS"
    assert second.get("duplicate") is True
    budget = await state.get("budget:marketing-2026Q2")
    assert Decimal(budget["remaining"]) == Decimal("400.00"), "budget was charged twice"
    assert len(saga_env["published"]) == 1


@pytest.mark.asyncio
async def test_a_non_positive_amount_is_refused(saga_env):
    """A zero or negative payment is a data problem, not something to execute."""
    state = saga_env["state"]
    await seed_budget(state, "marketing-2026Q2", "1000.00")

    result = await payment.execute_saga(event(amount_usd="0"))

    assert result["status"] == "FAILED"
    budget = await state.get("budget:marketing-2026Q2")
    assert Decimal(budget["remaining"]) == Decimal("1000.00")


# ── Insufficient budget ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unaffordable_payment_fails_without_touching_the_budget(saga_env):
    state = saga_env["state"]
    await seed_budget(state, "marketing-2026Q2", "100.00")

    result = await payment.execute_saga(event(amount_usd="600.00"))

    assert result["status"] == "FAILED"
    budget = await state.get("budget:marketing-2026Q2")
    assert Decimal(budget["remaining"]) == Decimal("100.00")
    assert [topic for topic, _ in saga_env["published"]] == ["payment.failed"]


@pytest.mark.asyncio
async def test_the_budget_can_never_go_negative(saga_env):
    """Section 7 of the policy: budgets are finite."""
    state = saga_env["state"]
    await seed_budget(state, "marketing-2026Q2", "1000.00")

    await payment.execute_saga(event(correlation_id=_cid(1), amount_usd="600.00"))
    await payment.execute_saga(event(correlation_id=_cid(2), amount_usd="600.00"))

    budget = await state.get("budget:marketing-2026Q2")
    assert Decimal(budget["remaining"]) >= Decimal("0")
    assert Decimal(budget["remaining"]) == Decimal("400.00")


# ── Compensation (M9) ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_payment_releases_the_reservation_and_restores_the_budget(saga_env):
    """Journey D: no orphaned reservation, no partial payment."""
    state = saga_env["state"]
    await seed_budget(state, "engineering-2026Q2", "50000.00")

    result = await payment.execute_saga(
        event(
            department="engineering-2026Q2",
            amount_usd="9500.00",
            invoice_number="RS-90021",  # forced failure
        )
    )

    assert result["status"] == "COMPENSATED"
    budget = await state.get("budget:engineering-2026Q2")
    assert Decimal(budget["remaining"]) == Decimal("50000.00"), "budget not restored"

    saga = await state.get("saga:11111111-1111-4111-8111-111111111111")
    assert saga["saga_state"] == "compensated"
    reservation = await state.get(f"reservation:{saga['saga_id']}")
    assert reservation["status"] == "released", "reservation left dangling"
    assert [topic for topic, _ in saga_env["published"]] == ["payment.compensated"]


@pytest.mark.asyncio
async def test_compensation_is_recorded_step_by_step_for_the_audit_trail(saga_env):
    state = saga_env["state"]
    await seed_budget(state, "engineering-2026Q2", "50000.00")

    await payment.execute_saga(
        event(department="engineering-2026Q2", amount_usd="9500.00", invoice_number="RS-90021")
    )

    attempt = saga_env["audit"][-1]
    assert attempt["outcome"] == "compensated"
    steps = {step["step"] for step in attempt["steps"]}
    assert {"S1", "S2", "C1"} <= steps
    assert attempt["error_message"]


@pytest.mark.asyncio
async def test_the_forced_failure_list_is_configuration_not_code(saga_env, monkeypatch):
    """Demonstrating a rollback must not require editing the service."""
    state = saga_env["state"]
    await seed_budget(state, "marketing-2026Q2", "1000.00")
    monkeypatch.setattr(payment, "FAIL_INVOICE_NUMBERS", {"EW-7001"})

    result = await payment.execute_saga(event())

    assert result["status"] == "COMPENSATED"
    budget = await state.get("budget:marketing-2026Q2")
    assert Decimal(budget["remaining"]) == Decimal("1000.00")


# ── Optimistic concurrency ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_lost_etag_race_is_retried_against_the_fresh_value(saga_env):
    """Two approvals landing together must not both write ``remaining - amount``."""
    state = saga_env["state"]
    await seed_budget(state, "marketing-2026Q2", "1000.00")
    state.conflicts_to_inject = 2  # the first two writes lose the race

    result = await payment.execute_saga(event(amount_usd="600.00"))

    assert result["status"] == "SUCCESS"
    budget = await state.get("budget:marketing-2026Q2")
    assert Decimal(budget["remaining"]) == Decimal("400.00"), (
        "the retry recomputed from a stale value"
    )


@pytest.mark.asyncio
async def test_unresolvable_contention_asks_for_redelivery_and_releases_the_claim(saga_env):
    """Better to retry the whole event than to guess at the budget."""
    state = saga_env["state"]
    await seed_budget(state, "marketing-2026Q2", "1000.00")
    state.conflicts_to_inject = 99

    result = await payment.execute_saga(event())

    assert result["status"] == "RETRY"
    # The idempotency claim is gone, so the redelivery can start cleanly.
    assert await state.get("saga:11111111-1111-4111-8111-111111111111") is None


@pytest.mark.asyncio
async def test_reserve_budget_raises_when_the_department_cannot_afford_it(fake_state, monkeypatch):
    monkeypatch.setattr(payment, "state", fake_state)
    await seed_budget(fake_state, "sales-2026Q2", "50.00")

    with pytest.raises(payment.InsufficientBudgetError):
        await payment._reserve_budget("sales-2026Q2", Decimal("100.00"))


@pytest.mark.asyncio
async def test_release_budget_survives_contention_and_reports_failure(fake_state, monkeypatch, caplog):
    """Failing to give money back is an operational alert, never a silent loss."""
    monkeypatch.setattr(payment, "state", fake_state)
    await seed_budget(fake_state, "sales-2026Q2", "100.00")
    fake_state.conflicts_to_inject = 99

    await payment._release_budget("sales-2026Q2", Decimal("50.00"), "saga-1")

    assert "COMPENSATION INCOMPLETE" in caplog.text


def test_update_atomic_gives_up_rather_than_looping_forever(fake_state):
    """A guard against a hot key spinning a request forever."""
    import asyncio

    fake_state.conflicts_to_inject = 99

    async def run():
        await fake_state.save("k", {"n": "1"})
        fake_state.conflicts_to_inject = 99
        await fake_state.update_atomic("k", lambda _current: {"n": "2"}, retries=2)

    with pytest.raises(ConcurrentUpdateError):
        asyncio.run(run())


# ── Service surface ─────────────────────────────────────────────────────────


def test_payment_subscribes_to_both_approval_paths():
    routes = {r.path for r in payment.app.routes}
    assert "/events/decision-auto-approved" in routes
    assert "/events/decision-human-approved" in routes
    assert "/internal/sagas/{correlation_id}" in routes
    assert "/internal/budgets" in routes


def _cid(n: int) -> str:
    return f"{n:08d}-1111-4111-8111-111111111111"
