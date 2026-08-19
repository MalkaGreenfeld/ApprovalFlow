"""Payment saga orchestrator with compensation (M9).

Three steps, each with a compensating action:

    S1  reserve department budget   ->  release it, restore the budget
    S2  execute payment            ->  void the payment
    S3  confirm and commit         ->  terminal, nothing left to undo

Budgets are Decimal, not float: repeated reserve/release cycles must not drift
by fractions of a cent in a system whose job is not overspending. Every budget
movement goes through an ETag compare-and-set loop that replays the arithmetic
against the fresh value, so two approvals landing together cannot both spend the
same money (the INV-1014A/B fixture).

Saga state lives in the Dapr state store so it survives a restart mid-flight;
an audit copy is written to PostgreSQL when the saga reaches a terminal state.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from approvalflow.dapr_client import ConcurrentUpdateError, DaprStateClient
from approvalflow.db import close_pool, get_pool, healthy, wait_for_pool
from approvalflow.events import PAYMENT_COMPENSATED, PAYMENT_COMPLETED, PAYMENT_FAILED
from approvalflow.logging import get_logger, set_correlation_id
from approvalflow.middleware import CorrelationIdMiddleware
from approvalflow.outbox import DaprEventPublisher

from . import repository as repo

logger = get_logger(__name__)

state = DaprStateClient()
publisher = DaprEventPublisher()

#: Invoice numbers whose payment is forced to fail, for the compensation journey.
#: Configuration rather than an ``if`` in the code path, so demonstrating a
#: rollback never means editing the service.
FAIL_INVOICE_NUMBERS: set[str] = {
    n.strip()
    for n in os.getenv("PAYMENT_FAIL_INVOICE_NUMBERS", "RS-90021").split(",")
    if n.strip()
}


def _payment_forced_to_fail(invoice_number: str) -> bool:
    """Whether this invoice is configured to fail at the gateway.

    Matches on containment rather than equality so that a verification run which
    scopes its invoice numbers (``RS-90021-3f9c12ab``, to stay independent of
    earlier runs) still exercises the compensation path.
    """
    return any(token in invoice_number for token in FAIL_INVOICE_NUMBERS)


class InsufficientBudgetError(Exception):
    """The department cannot fund this payment."""

    def __init__(self, department: str, requested: Decimal, remaining: Decimal):
        self.department = department
        self.requested = requested
        self.remaining = remaining
        super().__init__(
            f"Budget insufficient for {department}: requested ${requested}, "
            f"remaining ${remaining}"
        )


class PaymentFailedError(Exception):
    """The payment gateway rejected the payment."""

    def __init__(self, saga_id: str, reason: str):
        self.saga_id = saga_id
        self.reason = reason
        super().__init__(f"Payment failed for saga {saga_id}: {reason}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Seed budgets from the database into the state store, then serve.

    Startup deliberately tolerates an unavailable Dapr sidecar. The sidecar is
    configured to wait for *this* app's health endpoint, so insisting on the
    sidecar here would deadlock the pair: the app would exit, and because the
    sidecar shares the app's network namespace it would exit too. Seeding is
    retried, and a budget that still is not in the state store is resolved from
    the database on first use (see :func:`_budget_seed`).
    """
    await wait_for_pool()
    await _seed_budgets()
    logger.info(
        "Payment service ready (forced-failure invoices: %s)",
        ", ".join(sorted(FAIL_INVOICE_NUMBERS)) or "none",
    )
    try:
        yield
    finally:
        await close_pool()


app = FastAPI(
    title="ApprovalFlow Payment Service",
    version="1.0.0",
    description="Payment saga with budget reservation and compensating rollback.",
    lifespan=lifespan,
)
app.add_middleware(CorrelationIdMiddleware)


def _dec(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        logger.warning("Unparseable amount %r; treating it as %s", value, default)
        return default


def _budget_key(department: str) -> str:
    return f"budget:{department}"


def _saga_key(correlation_id: str) -> str:
    return f"saga:{correlation_id}"


async def _seed_budgets() -> None:
    """Copy the seeded budgets from PostgreSQL into the state store, once.

    The database row holds the initial value; the live counter is the state-store
    key, because that is where the ETag concurrency control lives.
    """
    try:
        budgets = await repo.seed_budgets(await get_pool())
    except Exception as exc:
        logger.error("Could not read seed budgets: %s", exc)
        return

    # The sidecar comes up shortly after this app starts listening, so a few
    # attempts spread over a few seconds is enough. Failure is logged, not fatal.
    for attempt in range(1, 6):
        try:
            for department, amount in budgets.items():
                key = _budget_key(department)
                if await state.get(key) is None:
                    await state.save(
                        key, {"department": department, "remaining": str(amount)}
                    )
                    logger.info("Seeded budget for %s: $%s", department, amount)
            return
        except Exception as exc:
            logger.warning(
                "State store not ready for budget seeding (attempt %s/5): %s",
                attempt, exc,
            )
            await asyncio.sleep(2)

    logger.warning(
        "Budgets were not pre-seeded into the state store; they will be resolved "
        "from the database on first use"
    )


_seed_cache: dict[str, Decimal] = {}


async def _budget_seed(department: str) -> Decimal:
    """The department's *initial* budget, straight from the database.

    Used when the state-store counter does not exist yet. Without this a missing
    key would read as ``remaining = 0`` and every payment would be rejected for
    insufficient budget, which is exactly what happened when the sidecar was not
    ready in time for startup seeding.
    """
    if department in _seed_cache:
        return _seed_cache[department]
    try:
        budgets = await repo.seed_budgets(await get_pool())
    except Exception as exc:
        logger.error("Could not read the seed budget for %s: %s", department, exc)
        return Decimal("0")
    _seed_cache.update({name: _dec(value) for name, value in budgets.items()})
    return _seed_cache.get(department, Decimal("0"))


# ── Health ──────────────────────────────────────────────────────────────────


@app.get("/health", tags=["ops"])
async def health():
    db_ok = await healthy()
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={
            "status": "ok" if db_ok else "degraded",
            "service": "payment",
            "database": "ok" if db_ok else "unavailable",
        },
    )


# ── The saga ────────────────────────────────────────────────────────────────


async def execute_saga(event: dict[str, Any]) -> dict[str, Any]:
    """Run the payment saga for one approved item.

    Idempotent: the first thing it does is claim ``saga:{correlation_id}``. A
    redelivered ``decision.*`` event, or an item approved twice, finds the claim
    and returns without touching the budget, the "no double payment" guarantee
    (M10).
    """
    correlation_id = str(event.get("correlation_id") or "unknown")
    set_correlation_id(correlation_id)

    amount = _dec(event.get("amount_usd"))
    department = str(event.get("department") or "unknown")
    invoice_number = str(event.get("invoice_number") or "")
    saga_id = str(uuid4())
    steps: list[dict[str, Any]] = []

    existing = await state.get(_saga_key(correlation_id))
    if existing is not None:
        logger.info(
            "Saga already exists (state=%s), no second payment",
            existing.get("saga_state"),
        )
        return {
            "status": "SUCCESS",
            "correlation_id": correlation_id,
            "duplicate": True,
            "saga_state": existing.get("saga_state"),
        }

    if amount <= 0:
        logger.error("Refusing to pay a non-positive amount (%s)", amount)
        await _finish(
            correlation_id, saga_id, department, amount, "failed",
            "invalid_amount", steps, f"amount {amount} is not payable",
        )
        await _publish(PAYMENT_FAILED, event, reason=f"invalid amount {amount}")
        return {"status": "FAILED", "correlation_id": correlation_id, "reason": "invalid amount"}

    saga = {
        "saga_id": saga_id,
        "correlation_id": correlation_id,
        "saga_state": "started",
        "current_step": "reserve_budget",
        "amount_usd": str(amount),
        "department": department,
        "started_at": datetime.now(UTC).isoformat(),
    }
    if not await state.save(_saga_key(correlation_id), saga):
        # No claim means no guarantee against a double payment: refuse to start
        # and let Dapr redeliver.
        logger.error("Could not persist the saga record; refusing to proceed")
        return {
            "status": "RETRY",
            "correlation_id": correlation_id,
            "detail": "failed to persist saga state",
        }

    logger.info(
        "Saga started", extra={"amount_usd": float(amount), "route": "payment"}
    )

    # ── S1: reserve budget (compare-and-set) ────────────────────────────────
    try:
        remaining_after = await _reserve_budget(department, amount)
        await state.save(
            f"reservation:{saga_id}",
            {
                "reservation_id": str(uuid4()),
                "saga_id": saga_id,
                "correlation_id": correlation_id,
                "department": department,
                "amount_usd": str(amount),
                "status": "reserved",
            },
        )
        saga.update(saga_state="budget_reserved", current_step="execute_payment")
        await state.save(_saga_key(correlation_id), saga)
        steps.append(
            {"step": "S1", "action": "reserve_budget", "result": "ok",
             "remaining_after": str(remaining_after)}
        )
        logger.info("S1 reserved $%s, %s has $%s left", amount, department, remaining_after)
    except InsufficientBudgetError as exc:
        steps.append({"step": "S1", "action": "reserve_budget", "result": "insufficient_budget"})
        saga.update(saga_state="payment_failed", error_message=str(exc))
        await state.save(_saga_key(correlation_id), saga)
        await _finish(correlation_id, saga_id, department, amount, "failed",
                      "payment_failed", steps, str(exc))
        await _publish(PAYMENT_FAILED, event, reason=str(exc))
        logger.error("S1 failed: %s", exc)
        return {"status": "FAILED", "correlation_id": correlation_id, "reason": str(exc)}
    except ConcurrentUpdateError as exc:
        # Contention, not a business failure. Release the idempotency claim so the
        # redelivered event can try again cleanly, leaving the claim in place
        # would make a transient conflict look like a completed saga.
        steps.append({"step": "S1", "action": "reserve_budget", "result": "contention"})
        await state.delete(_saga_key(correlation_id))
        logger.error("S1 could not settle under contention: %s", exc)
        return {"status": "RETRY", "correlation_id": correlation_id, "reason": str(exc)}

    # ── S2: execute payment ─────────────────────────────────────────────────
    try:
        if _payment_forced_to_fail(invoice_number):
            raise PaymentFailedError(saga_id, "Simulated payment gateway failure")
        saga.update(saga_state="payment_executed", current_step="confirm")
        await state.save(_saga_key(correlation_id), saga)
        steps.append({"step": "S2", "action": "execute_payment", "result": "ok"})
        logger.info("S2 payment executed")
    except PaymentFailedError as exc:
        steps.append({"step": "S2", "action": "execute_payment", "result": "failed"})
        released = await _release_budget(department, amount, saga_id)
        steps.append(
            {"step": "C1", "action": "release_reservation", "result": "ok",
             "remaining_after": str(released)}
        )
        saga.update(saga_state="compensated", error_message=str(exc))
        await state.save(_saga_key(correlation_id), saga)
        await _finish(correlation_id, saga_id, department, amount, "compensated",
                      "compensated", steps, str(exc))
        await _publish(PAYMENT_COMPENSATED, event, reason=str(exc))
        logger.error("S2 failed, compensated: %s", exc)
        return {"status": "COMPENSATED", "correlation_id": correlation_id, "reason": str(exc)}

    # ── S3: confirm ─────────────────────────────────────────────────────────
    try:
        reservation = await state.get(f"reservation:{saga_id}") or {}
        reservation["status"] = "committed"
        if not await state.save(f"reservation:{saga_id}", reservation):
            raise RuntimeError("could not commit the reservation record")
        saga.update(saga_state="confirmed", current_step="complete",
                    completed_at=datetime.now(UTC).isoformat())
        if not await state.save(_saga_key(correlation_id), saga):
            raise RuntimeError("could not mark the saga confirmed")
        steps.append({"step": "S3", "action": "confirm", "result": "ok"})
    except Exception as exc:
        steps.append({"step": "S3", "action": "confirm", "result": "failed"})
        await _void_payment(saga_id)
        released = await _release_budget(department, amount, saga_id)
        steps.append(
            {"step": "C2", "action": "void_and_release", "result": "ok",
             "remaining_after": str(released)}
        )
        saga.update(saga_state="compensated", error_message=str(exc))
        await state.save(_saga_key(correlation_id), saga)
        await _finish(correlation_id, saga_id, department, amount, "compensated",
                      "compensated", steps, str(exc))
        await _publish(PAYMENT_COMPENSATED, event, reason=str(exc))
        logger.exception("S3 failed, compensated: %s", exc)
        return {"status": "COMPENSATED", "correlation_id": correlation_id, "reason": str(exc)}

    await _finish(correlation_id, saga_id, department, amount, "confirmed",
                  "confirmed", steps, None)
    await _publish(PAYMENT_COMPLETED, event)
    logger.info("Saga confirmed, payment complete")
    return {"status": "SUCCESS", "correlation_id": correlation_id, "saga_id": saga_id}


async def _reserve_budget(department: str, amount: Decimal) -> Decimal:
    """Atomically decrement the department budget.

    Raises:
        InsufficientBudgetError: when the budget cannot cover the amount. Raised
            from inside the CAS loop, so it is evaluated against the *fresh* value
            on every retry, which is what makes the no-overspend guarantee hold
            under concurrency.
        ConcurrentUpdateError: when contention never settles.
    """

    # Resolved before the loop so the CAS body stays free of I/O.
    seed = await _budget_seed(department)

    def mutate(current: dict[str, Any] | None) -> dict[str, Any]:
        # An absent counter means "not seeded yet", not "no money left".
        remaining = _dec(current["remaining"]) if current else seed
        if remaining < amount:
            raise InsufficientBudgetError(department, amount, remaining)
        return {"department": department, "remaining": str(remaining - amount)}

    result = await state.update_atomic(_budget_key(department), mutate)
    return _dec(result.get("remaining"))


async def _release_budget(department: str, amount: Decimal, saga_id: str) -> Decimal:
    """Compensating action for S1: give the money back and release the hold.

    Uses the same CAS loop, so a release racing another reservation cannot lose
    either update.
    """

    def mutate(current: dict[str, Any] | None) -> dict[str, Any]:
        remaining = _dec((current or {}).get("remaining"))
        return {"department": department, "remaining": str(remaining + amount)}

    try:
        result = await state.update_atomic(_budget_key(department), mutate)
    except ConcurrentUpdateError as exc:
        # Losing the money back is worse than any other failure here: log it as
        # an operational alert rather than swallowing it.
        logger.error(
            "COMPENSATION INCOMPLETE, could not restore $%s to %s: %s",
            amount, department, exc,
        )
        return _dec(None)

    reservation = await state.get(f"reservation:{saga_id}") or {"saga_id": saga_id}
    reservation.update(status="released", department=department, amount_usd=str(amount))
    await state.save(f"reservation:{saga_id}", reservation)
    return _dec(result.get("remaining"))


async def _void_payment(saga_id: str) -> None:
    """Compensating action for S2."""
    reservation = await state.get(f"reservation:{saga_id}")
    if reservation:
        reservation["status"] = "voided"
        await state.save(f"reservation:{saga_id}", reservation)


async def _finish(
    correlation_id: str,
    saga_id: str,
    department: str,
    amount: Decimal,
    outcome: str,
    final_state: str,
    steps: list[dict[str, Any]],
    error: str | None,
) -> None:
    """Write the durable audit copy of the saga (F9)."""
    try:
        await repo.record_attempt(
            await get_pool(),
            correlation_id=correlation_id,
            saga_id=saga_id,
            department=department,
            amount_usd=amount,
            outcome=outcome,
            final_saga_state=final_state,
            steps=steps,
            error_message=error,
        )
    except Exception as exc:
        logger.error("Could not write the payment audit record: %s", exc)


async def _publish(topic: str, event: dict[str, Any], reason: str | None = None) -> None:
    """Publish a payment outcome event."""
    payload: dict[str, Any] = {
        "correlation_id": event.get("correlation_id"),
        "amount_usd": str(_dec(event.get("amount_usd"))),
        "department": event.get("department", ""),
        "invoice_number": event.get("invoice_number", ""),
        "submitter_email": event.get("submitter_email", ""),
    }
    if reason:
        payload["reason"] = reason
    if not await publisher.publish(topic, payload):
        # The saga is already durable in the state store and the audit table; a
        # failed notification must be visible, not silent.
        logger.error("Could not publish %s for %s", topic, payload["correlation_id"])


# ── Subscriptions ───────────────────────────────────────────────────────────


def _extract_event(body: dict[str, Any]) -> dict[str, Any]:
    payload = body.get("data", body)
    return payload.get("data", payload) if isinstance(payload, dict) else body


@app.post("/events/decision-auto-approved", tags=["events"])
async def on_auto_approved(request: Request):
    """Pay an item the router approved autonomously."""
    result = await execute_saga(_extract_event(await request.json()))
    return JSONResponse(
        status_code=500 if result.get("status") == "RETRY" else 200, content=result
    )


@app.post("/events/decision-human-approved", tags=["events"])
async def on_human_approved(request: Request):
    """Pay an item a human approved."""
    result = await execute_saga(_extract_event(await request.json()))
    return JSONResponse(
        status_code=500 if result.get("status") == "RETRY" else 200, content=result
    )


# ── Internal API (audit trail via Dapr service invocation) ──────────────────


@app.get("/internal/sagas/{correlation_id}", tags=["internal"])
async def internal_saga(correlation_id: str):
    """The saga, its reservation and the audit rows for one item (F9)."""
    saga = await state.get(_saga_key(correlation_id))
    reservation = None
    if saga and saga.get("saga_id"):
        reservation = await state.get(f"reservation:{saga['saga_id']}")
    return {
        "saga": saga,
        "reservation": reservation,
        "attempts": await repo.attempts(await get_pool(), correlation_id),
    }


@app.get("/internal/budgets", tags=["internal"])
async def internal_budgets():
    """Live budget counters, used by the verification script to prove no overspend."""
    budgets = await repo.seed_budgets(await get_pool())
    live: dict[str, str] = {}
    for department in budgets:
        record = await state.get(_budget_key(department))
        live[department] = str(_dec((record or {}).get("remaining")))
    return {"budgets": live, "seeded": {k: str(v) for k, v in budgets.items()}}
