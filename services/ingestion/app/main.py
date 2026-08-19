"""Submission intake and the submission's system of record.

Owns:

* POST /api/submissions: validate, persist and enqueue in one transaction, and
  return 202 with a tracking id immediately (F1 / M8);
* duplicate detection at intake, so a re-submission never reaches the payment
  path (F3 / M10);
* the submission status and its plain-language reason (F2);
* the submitter's side of send-back-for-more-info (F5);
* the decision trail for auditors (F9), built from this service's timeline plus
  the other services' records fetched over Dapr service invocation.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from approvalflow.auth import (
    ROLE_ADMIN,
    ROLE_APPROVER,
    ROLE_SUBMITTER,
    Principal,
    issue_token,
    log_auth_mode,
    require_roles,
)
from approvalflow.dapr_client import DaprInvokeClient, DaprStateClient
from approvalflow.db import close_pool, get_pool, healthy, wait_for_pool
from approvalflow.events import SUBMISSION_INFO_PROVIDED, SUBMISSION_RECEIVED
from approvalflow.idgen import uuid7
from approvalflow.logging import get_logger, set_correlation_id
from approvalflow.middleware import CorrelationIdMiddleware
from approvalflow.models import SubmissionRequest
from approvalflow.outbox import DaprEventPublisher, OutboxDispatcher, enqueue
from approvalflow.policy import PolicyConfigStore

from . import repository as repo
from .validation import (
    convert_to_usd,
    make_idempotency_key,
    math_reconciles,
    whitelist_amendments,
)

logger = get_logger(__name__)

OUTBOX_TABLE = "ingestion.outbox"

state = DaprStateClient()
invoke = DaprInvokeClient()
policy_store = PolicyConfigStore(state)
publisher = DaprEventPublisher()
dispatcher = OutboxDispatcher(get_pool, publisher, table=OUTBOX_TABLE)

#: Decision/payment event → the status it drives. Ingestion is the only writer of
#: submission status, so this table is the whole state machine in one place.
STATUS_BY_EVENT: dict[str, str] = {
    "decision.auto_approved": "auto_approved",
    "decision.escalated": "human_review",
    "decision.rejected": "rejected",
    "decision.duplicate": "duplicate",
    "decision.human_approved": "human_approved",
    "decision.human_rejected": "human_rejected",
    "decision.info_requested": "info_requested",
    "payment.completed": "paid",
    "payment.failed": "payment_failed",
    "payment.compensated": "compensated",
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log_auth_mode("ingestion")
    await wait_for_pool()
    config = await policy_store.get()
    logger.info(
        "Ingestion ready, policy config v%s, %s FX rates configured",
        config.version,
        len(config.fx_rates),
    )
    dispatcher.start()
    try:
        yield
    finally:
        await dispatcher.stop()
        await close_pool()


app = FastAPI(
    title="ApprovalFlow Ingestion Service",
    version="1.0.0",
    description=(
        "Submission intake, status, information requests and the auditor's "
        "decision trail."
    ),
    lifespan=lifespan,
)
app.add_middleware(CorrelationIdMiddleware)


# ── Health ──────────────────────────────────────────────────────────────────


@app.get("/health", tags=["ops"])
async def health():
    """Liveness plus database reachability, so Dapr stops routing to a broken pod."""
    db_ok = await healthy()
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={
            "status": "ok" if db_ok else "degraded",
            "service": "ingestion",
            "database": "ok" if db_ok else "unavailable",
        },
    )


@app.get("/health/outbox", tags=["ops"])
async def outbox_health():
    """Outbox depth, a growing ``pending`` count means pub/sub is in trouble."""
    return await dispatcher.stats()


# ── Dev token endpoint (N1) ─────────────────────────────────────────────────


class TokenRequest(BaseModel):
    """Request for a development token.

    A real deployment would federate to an identity provider; the assignment
    allows a self-signed JWT, so this endpoint exists to make the roles
    demonstrable. It never grants a role the caller did not ask for and it is the
    only place in the system that mints tokens.
    """

    subject: str = Field(default="demo@northwind.example")
    roles: list[str] = Field(default_factory=lambda: [ROLE_SUBMITTER])


@app.post("/api/auth/token", tags=["auth"])
async def create_token(req: TokenRequest):
    """Mint a signed JWT for the requested roles (development identity provider)."""
    try:
        token, expires_in = await issue_token(req.subject, req.roles, email=req.subject)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    logger.info("Issued token for %s with roles %s", req.subject, req.roles)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": expires_in,
        "roles": req.roles,
    }


# ── Submission intake ───────────────────────────────────────────────────────


@app.post("/api/submissions", status_code=status.HTTP_202_ACCEPTED, tags=["submissions"])
async def create_submission(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(require_roles(ROLE_SUBMITTER)),
):
    """Accept a submission and return a tracking id immediately (F1 / M8).

    Everything that must not diverge happens in a single transaction:

    1. an advisory lock on the business key serialises duplicate detection;
    2. the submission row is inserted;
    3. the ``submission.received`` event is written to the outbox.

    So there is no window in which a stored submission has no event, or an event
    exists for a submission that was never stored. The response is 202 with the
    correlation id, the caller never waits for the agent.
    """
    try:
        body = await request.json()
        req = SubmissionRequest.model_validate(body)
    except Exception as exc:
        logger.warning("Rejected invalid submission payload: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid submission payload",
        ) from exc

    config = await policy_store.get()
    # UUIDv7: time-ordered, so correlation_id-keyed inserts stay at the right-hand
    # edge of the index instead of scattering across it.
    correlation_id = str(uuid7())
    set_correlation_id(correlation_id)

    business_key = make_idempotency_key(req.vendor, req.invoiceNumber, req.total)
    line_items_total = sum(
        (item.unit_price * item.quantity for item in req.lineItems), start=Decimal("0")
    )
    amount_usd = convert_to_usd(req.total, req.currency, config)
    math_ok = math_reconciles(
        line_items_total, req.taxAmount, req.total, line_item_count=len(req.lineItems)
    )
    line_items = [
        {
            "description": item.description,
            "quantity": item.quantity,
            "unitPrice": str(item.unit_price),
        }
        for item in req.lineItems
    ]

    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        # HTTP-level retry protection: a client resending the identical
        # request replays the original answer instead of creating a twin.
        if idempotency_key:
            replayed = await repo.replay_response(conn, idempotency_key)
            if replayed is not None:
                logger.info("Replaying stored response for Idempotency-Key")
                return JSONResponse(status_code=200, content=replayed)

        await repo.lock_business_key(conn, business_key)
        prior = await repo.find_prior_submission(
            conn, req.vendor, req.invoiceNumber, req.total
        )
        duplicate_of: str | None = None
        if prior is not None:
            duplicate_of = str(prior["correlation_id"])
            logger.info(
                "Business duplicate of %s (status=%s), will be short-circuited",
                duplicate_of,
                prior["status"],
            )

        await repo.insert_submission(
            conn,
            {
                "correlation_id": correlation_id,
                "submitter_email": req.submitter,
                "department": req.department,
                "vendor": req.vendor,
                "vendor_known": req.vendorKnown,
                "invoice_number": req.invoiceNumber,
                "currency": req.currency.upper(),
                "amount_original": req.total,
                "amount_usd": amount_usd,
                "category": req.category.lower(),
                "attendees": req.attendees,
                "line_items": line_items,
                "tax_amount": req.taxAmount,
                "total": req.total,
                "receipt_present": req.receiptPresent,
                "math_ok": math_ok,
                "raw_payload": body,
                "notes": req.notes,
                "status": "received",
                "idempotency_key": business_key,
                "duplicate_of": duplicate_of,
            },
        )
        await repo.record_event(
            conn,
            correlation_id=correlation_id,
            event_type="submitted",
            to_status="received",
            actor=principal.email or req.submitter,
            detail={
                "amount_usd": str(amount_usd),
                "math_ok": math_ok,
                "duplicate_of": duplicate_of,
                "policy_config_version": config.version,
            },
        )
        await enqueue(
            conn,
            correlation_id=correlation_id,
            topic=SUBMISSION_RECEIVED,
            payload=_submission_event(
                correlation_id=correlation_id,
                req=req,
                amount_usd=amount_usd,
                line_items=line_items,
                math_ok=math_ok,
                duplicate_of=duplicate_of,
                revision=0,
            ),
            table=OUTBOX_TABLE,
        )

        response_body: dict[str, Any] = {
            "correlation_id": correlation_id,
            "status": "received",
            "tracking_url": f"/api/submissions/{correlation_id}/status",
            "idempotency_key": business_key,
            "duplicate_of": duplicate_of,
        }
        if idempotency_key:
            await repo.remember_response(
                conn, idempotency_key, correlation_id, response_body
            )

    logger.info(
        "Submission accepted",
        extra={"correlation_id": correlation_id, "amount_usd": float(amount_usd)},
    )
    # Nudge the dispatcher so the happy path has no polling latency; durability
    # already came from the transaction above.
    await dispatcher.dispatch_once()
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=response_body)


def _submission_event(
    *,
    correlation_id: str,
    req: SubmissionRequest,
    amount_usd: Decimal,
    line_items: list[dict[str, Any]],
    math_ok: bool,
    duplicate_of: str | None,
    revision: int,
) -> dict[str, Any]:
    """Build the ``submission.received`` payload.

    ``submitted_amount_usd`` and ``submitted_category`` are carried separately
    from ``amount_usd``/``category`` so the router can enforce against the intake
    figures even though the agent restates them downstream.
    """
    return {
        "correlation_id": correlation_id,
        "revision": revision,
        "submitter_email": req.submitter,
        "department": req.department,
        "vendor": req.vendor,
        "vendor_known": req.vendorKnown,
        "invoice_number": req.invoiceNumber,
        "currency": req.currency.upper(),
        "amount_original": str(req.total),
        "amount_usd": str(amount_usd),
        "submitted_amount_usd": str(amount_usd),
        "category": req.category.lower(),
        "submitted_category": req.category.lower(),
        "attendees": req.attendees,
        "line_items": line_items,
        "tax_amount": str(req.taxAmount),
        "total": str(req.total),
        "receipt_present": req.receiptPresent,
        "notes": req.notes,
        "date": req.date,
        "math_ok": math_ok,
        "duplicate_of": duplicate_of,
        "is_duplicate": duplicate_of is not None,
    }


# ── Status and audit ────────────────────────────────────────────────────────


@app.get("/api/submissions/{correlation_id}/status", tags=["submissions"])
async def get_status(correlation_id: str):
    """Current status plus the plain-language reason for the outcome (F2)."""
    record = await repo.get_submission(await get_pool(), correlation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Submission not found")

    return {
        "correlation_id": correlation_id,
        "status": record["status"],
        "reason": record.get("decision_reason") or _default_reason(record["status"]),
        "rule_ids": record.get("rule_ids", []),
        "vendor": record["vendor"],
        "amount_usd": record["amount_usd"],
        "currency": record["currency"],
        "category": record["category"],
        "revision": record["revision"],
        "duplicate_of": record.get("duplicate_of"),
        # What the approver is waiting for, so the submitter can act (F5).
        "info_request": record.get("open_info_request"),
        "info_exchange": record.get("info_exchange", []),
        "submitted_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def _default_reason(status_value: str) -> str:
    """Fallback wording for statuses that precede any decision."""
    return {
        "received": "Received and queued for analysis.",
        "reanalyzing": "Your additional information is being re-analysed.",
    }.get(status_value, "Processing.")


@app.get("/api/submissions/{correlation_id}/audit", tags=["audit"])
async def get_audit_trail(
    correlation_id: str,
    _p: Principal = Depends(require_roles(ROLE_ADMIN, ROLE_APPROVER)),
):
    """The complete decision trail for one item, by correlation id (F9).

    Assembled from four owners, each asked for its own records over Dapr service
    invocation rather than by reaching into another service's tables:

    * ingestion: the extracted data and the status timeline;
    * router: every decision round, the rules applied and who ruled;
    * notification: the escalation, the information requests and the answers;
    * payment: the saga and its compensation outcome.
    """
    submission = await repo.get_submission(await get_pool(), correlation_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")

    decisions = await invoke.call(
        "router", f"internal/decisions/{correlation_id}", http_verb="GET"
    )
    approvals = await invoke.call(
        "notification", f"internal/approvals/{correlation_id}", http_verb="GET"
    )
    payment = await invoke.call(
        "payment", f"internal/sagas/{correlation_id}", http_verb="GET"
    )

    return {
        "correlation_id": correlation_id,
        "extracted_data": {
            key: submission[key]
            for key in (
                "submitter_email", "department", "vendor", "vendor_known",
                "invoice_number", "currency", "amount_original", "amount_usd",
                "category", "attendees", "line_items", "tax_amount", "total",
                "receipt_present", "math_ok", "notes",
            )
            if key in submission
        },
        "current_status": submission["status"],
        "revision": submission["revision"],
        "duplicate_of": submission.get("duplicate_of"),
        "timeline": await repo.timeline(await get_pool(), correlation_id),
        "decisions": (decisions or {}).get("decisions", []),
        "human_review": approvals or {},
        "payment": payment or {},
        "info_exchange": submission.get("info_exchange", []),
        # Flag rather than hide a partial trail: an auditor must know a source
        # was unreachable instead of concluding nothing happened there.
        "sources_unavailable": [
            name
            for name, value in (
                ("router", decisions),
                ("notification", approvals),
                ("payment", payment),
            )
            if value is None
        ],
    }


@app.get("/api/metrics/submissions", tags=["reports"])
async def submission_metrics(
    window_hours: int = Query(default=24, ge=1, le=720),
    _p: Principal = Depends(require_roles(ROLE_ADMIN, ROLE_APPROVER)),
):
    """Submission-side throughput counters for the dashboard (F8)."""
    return await repo.throughput(await get_pool(), window_hours)


# ── Answering an information request (F5, submitter side) ───────────────────


class InfoResponse(BaseModel):
    """A submitter's answer to an approver's request for more information."""

    answer: str = Field(default="", description="Free-text reply to the question")
    updates: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Corrections to the submission, restricted to the configured "
            "amendable_fields. Amount changes are allowed but re-run the full "
            "decision chain, so they cannot be used to slip under a ceiling."
        ),
    )


@app.post("/api/submissions/{correlation_id}/info-response", tags=["submissions"])
async def provide_info(
    correlation_id: str,
    payload: InfoResponse,
    principal: Principal = Depends(require_roles(ROLE_SUBMITTER)),
):
    """Answer the open information request and resume the paused workflow.

    The submission keeps its original correlation id and gains a revision, so the
    approver's question, the submitter's answer and the fresh analysis all hang
    off one audit trail. The amended row and the ``submission.info_provided``
    event are written in one transaction, so a resume cannot be lost.
    """
    set_correlation_id(correlation_id)
    config = await policy_store.get()
    updates, rejected = whitelist_amendments(payload.updates, config)
    if rejected:
        logger.warning("Rejected non-amendable fields: %s", rejected)

    pool = await get_pool()
    try:
        async with pool.acquire() as conn, conn.transaction():
            amended = await repo.apply_info_response(
                conn,
                correlation_id,
                updates=updates,
                answer={"text": payload.answer, "updates": updates},
                actor=principal.email or principal.subject,
            )
            # Recompute the derived facts on the amended figures.
            line_items = amended.get("line_items") or []
            line_items_total = sum(
                (
                    Decimal(str(i.get("unitPrice", 0))) * int(i.get("quantity", 1))
                    for i in line_items
                ),
                start=Decimal("0"),
            )
            total = Decimal(str(amended["total"]))
            amount_usd = convert_to_usd(total, str(amended["currency"]), config)
            math_ok = math_reconciles(
                line_items_total,
                Decimal(str(amended["tax_amount"])),
                total,
                line_item_count=len(line_items),
            )
            await conn.execute(
                """
                    UPDATE ingestion.submissions
                    SET amount_usd = $2, math_ok = $3, updated_at = NOW()
                    WHERE correlation_id = $1::uuid
                    """,
                correlation_id,
                amount_usd,
                math_ok,
            )
            await enqueue(
                conn,
                correlation_id=correlation_id,
                topic=SUBMISSION_INFO_PROVIDED,
                payload={
                    "correlation_id": correlation_id,
                    "revision": int(amended["revision"]),
                    "submitter_email": amended["submitter_email"],
                    "department": amended["department"],
                    "vendor": amended["vendor"],
                    "vendor_known": amended["vendor_known"],
                    "invoice_number": amended["invoice_number"],
                    "currency": amended["currency"],
                    "amount_usd": str(amount_usd),
                    "submitted_amount_usd": str(amount_usd),
                    "amount_original": str(total),
                    "category": amended["category"],
                    "submitted_category": amended["category"],
                    "attendees": amended["attendees"],
                    "line_items": line_items,
                    "tax_amount": str(amended["tax_amount"]),
                    "total": str(total),
                    "receipt_present": amended["receipt_present"],
                    "notes": amended["notes"],
                    "math_ok": math_ok,
                    "duplicate_of": amended.get("duplicate_of"),
                    "is_duplicate": amended.get("duplicate_of") is not None,
                    "info_exchange": amended.get("info_exchange", []),
                },
                table=OUTBOX_TABLE,
            )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await dispatcher.dispatch_once()
    logger.info("Information provided; workflow resuming at revision %s",
                amended["revision"])
    return {
        "status": "resumed",
        "correlation_id": correlation_id,
        "revision": amended["revision"],
        "rejected_fields": rejected,
    }


# ── Event subscribers: ingestion owns submission status ─────────────────────


def _extract_event(body: dict[str, Any]) -> dict[str, Any]:
    payload = body.get("data", body)
    return payload.get("data", payload) if isinstance(payload, dict) else body


async def _handle_status_event(request: Request, event_name: str) -> JSONResponse:
    """Shared handler for every decision/payment event.

    One implementation instead of ten near-identical copies: the mapping from
    event to status is data (:data:`STATUS_BY_EVENT`), and the reason and rules
    from the decision are stored for the status endpoint (F2).
    """
    event = _extract_event(await request.json())
    correlation_id = event.get("correlation_id")
    if not correlation_id:
        logger.error("%s without a correlation_id; dropping", event_name)
        return JSONResponse(content={"status": "DROP"})
    set_correlation_id(correlation_id)

    new_status = STATUS_BY_EVENT[event_name]

    # Attach the request box *before* moving to info_requested. The two writes are
    # separate statements, so doing it the other way round leaves a window where a
    # client polling the status sees "we need more information" without being able
    # to see what is actually being asked for.
    if event_name == "decision.info_requested":
        await repo.open_info_request(
            await get_pool(),
            correlation_id,
            {
                "requested_by": event.get("approver_email") or event.get("decided_by"),
                "requested_fields": event.get("requested_fields", []),
                "question": event.get("question") or event.get("approver_comment") or "",
                "requested_at_revision": int(event.get("revision", 0) or 0),
            },
        )

    changed, previous = await repo.update_status(
        await get_pool(),
        correlation_id,
        new_status,
        event_type=event_name,
        actor=str(event.get("decided_by") or "system"),
        detail={
            "reason": event.get("reason"),
            "rule_ids": event.get("rule_ids", []),
            "route": event.get("route"),
            "policy_config_version": event.get("policy_config_version"),
        },
    )

    if changed:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ingestion.submissions
                SET decision_reason = COALESCE($2, decision_reason),
                    rule_ids = $3::text[]
                WHERE correlation_id = $1::uuid
                """,
                correlation_id,
                event.get("reason"),
                list(event.get("rule_ids") or []),
            )

    logger.info(
        "%s applied (%s -> %s)", event_name, previous, new_status if changed else previous
    )
    # Always acknowledge: a redelivery that changes nothing is success, not an
    # error, and NACKing it would loop the message forever.
    return JSONResponse(
        content={"status": "SUCCESS", "correlation_id": correlation_id, "changed": changed}
    )


@app.post("/events/decision-auto-approved", tags=["events"])
async def on_auto_approved(request: Request):
    return await _handle_status_event(request, "decision.auto_approved")


@app.post("/events/decision-escalated", tags=["events"])
async def on_escalated(request: Request):
    return await _handle_status_event(request, "decision.escalated")


@app.post("/events/decision-rejected", tags=["events"])
async def on_rejected(request: Request):
    return await _handle_status_event(request, "decision.rejected")


@app.post("/events/decision-duplicate", tags=["events"])
async def on_duplicate(request: Request):
    return await _handle_status_event(request, "decision.duplicate")


@app.post("/events/decision-human-approved", tags=["events"])
async def on_human_approved(request: Request):
    return await _handle_status_event(request, "decision.human_approved")


@app.post("/events/decision-human-rejected", tags=["events"])
async def on_human_rejected(request: Request):
    return await _handle_status_event(request, "decision.human_rejected")


@app.post("/events/decision-info-requested", tags=["events"])
async def on_info_requested(request: Request):
    return await _handle_status_event(request, "decision.info_requested")


@app.post("/events/payment-completed", tags=["events"])
async def on_payment_completed(request: Request):
    return await _handle_status_event(request, "payment.completed")


@app.post("/events/payment-failed", tags=["events"])
async def on_payment_failed(request: Request):
    return await _handle_status_event(request, "payment.failed")


@app.post("/events/payment-compensated", tags=["events"])
async def on_payment_compensated(request: Request):
    return await _handle_status_event(request, "payment.compensated")
