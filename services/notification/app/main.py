"""Outcome delivery and the human-in-the-loop queue.

Owns the approver's side of the workflow:

* the escalation queue, holding only escalated items, each with the agent's
  recommendation, its confidence and the rules the router cited (F4);
* approve, reject or send back in one action. A send-back carries a structured
  request (which fields are missing, and the question to answer) so the submitter
  is told what is actually needed (F5);
* the durable pause record in the Dapr state store, which is what lets the
  workflow resume after a restart (M11);
* the record that the submitter was notified (M8).

Ingestion owns submissions.status, not this service: two services writing one
field from the same events would make the final value a race.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from approvalflow.auth import (
    ROLE_ADMIN,
    ROLE_APPROVER,
    Principal,
    log_auth_mode,
    require_roles,
)
from approvalflow.dapr_client import DaprStateClient
from approvalflow.db import close_pool, get_pool, healthy, wait_for_pool
from approvalflow.events import APPROVAL_ACTION_RECEIVED
from approvalflow.logging import get_logger, set_correlation_id
from approvalflow.middleware import CorrelationIdMiddleware
from approvalflow.outbox import DaprEventPublisher, OutboxDispatcher, enqueue

from . import repository as repo

logger = get_logger(__name__)

OUTBOX_TABLE = "notification.outbox"

state = DaprStateClient()
publisher = DaprEventPublisher()
dispatcher = OutboxDispatcher(get_pool, publisher, table=OUTBOX_TABLE)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log_auth_mode("notification")
    await wait_for_pool()
    dispatcher.start()
    logger.info("Notification service ready")
    try:
        yield
    finally:
        await dispatcher.stop()
        await close_pool()


app = FastAPI(
    title="ApprovalFlow Notification Service",
    version="1.0.0",
    description="Approver queue, information requests and outcome notifications.",
    lifespan=lifespan,
)
app.add_middleware(CorrelationIdMiddleware)


def _extract_event(body: dict[str, Any]) -> dict[str, Any]:
    payload = body.get("data", body)
    return payload.get("data", payload) if isinstance(payload, dict) else body


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


@app.get("/health", tags=["ops"])
async def health():
    db_ok = await healthy()
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={
            "status": "ok" if db_ok else "degraded",
            "service": "notification",
            "database": "ok" if db_ok else "unavailable",
        },
    )


# ── The durable HITL pause token (M11) ──────────────────────────────────────


def _pause_key(correlation_id: str) -> str:
    return f"hitl:{correlation_id}"


async def _pause(correlation_id: str, context: dict[str, Any]) -> None:
    """Record that the workflow is waiting for a human, in the state store.

    Nothing is held in memory: the token, the queue row and the submission are
    all durable, so a restart between "escalated" and "approved" changes nothing
    about where the workflow resumes.
    """
    await state.save(
        _pause_key(correlation_id),
        {
            "correlation_id": correlation_id,
            "paused_at": datetime.now(UTC).isoformat(),
            "awaiting": "human_decision",
            **context,
        },
    )


async def _resume(correlation_id: str, resolution: str, actor: str) -> None:
    """Close the pause token once a human has acted."""
    token = await state.get(_pause_key(correlation_id)) or {}
    token.update(
        {
            "awaiting": None,
            "resolved_as": resolution,
            "resolved_by": actor,
            "resumed_at": datetime.now(UTC).isoformat(),
        }
    )
    await state.save(_pause_key(correlation_id), token)


# ── Event handlers ──────────────────────────────────────────────────────────


@app.post("/events/decision-escalated", tags=["events"])
async def on_escalated(request: Request):
    """Put an escalated item on the queue and pause the workflow durably."""
    event = _extract_event(await request.json())
    cid = event.get("correlation_id")
    if not cid:
        return JSONResponse(content={"status": "DROP"})
    set_correlation_id(cid)

    pool = await get_pool()
    await repo.upsert_queue_item(
        pool,
        {
            "correlation_id": cid,
            "revision": int(event.get("revision", 0) or 0),
            "vendor": event.get("vendor"),
            "submitter_email": event.get("submitter_email"),
            "department": event.get("department"),
            "category": event.get("category"),
            "amount_usd": _dec(event.get("amount_usd")),
            "agent_recommendation": event.get("agent_recommendation"),
            "agent_confidence": _dec(event.get("agent_confidence")),
            "agent_reasoning": event.get("agent_reasoning"),
            "rule_ids": event.get("rule_ids", []),
            "escalation_reason": event.get("reason"),
        },
    )
    await _pause(
        cid,
        {
            "revision": int(event.get("revision", 0) or 0),
            "reason": event.get("reason"),
            "rule_ids": event.get("rule_ids", []),
        },
    )
    await _notify(
        cid,
        recipient=str(event.get("submitter_email") or ""),
        subject="Your submission needs a manager's approval",
        body=str(event.get("reason") or "Escalated for human review."),
        event_type="decision.escalated",
    )
    logger.info("Escalation queued and workflow paused")
    return JSONResponse(content={"status": "SUCCESS"})


@app.post("/events/decision-auto-approved", tags=["events"])
async def on_auto_approved(request: Request):
    return await _notify_only(request, "decision.auto_approved",
                              "Your submission was approved automatically")


@app.post("/events/decision-rejected", tags=["events"])
async def on_rejected(request: Request):
    return await _notify_only(request, "decision.rejected",
                              "Your submission was rejected")


@app.post("/events/decision-duplicate", tags=["events"])
async def on_duplicate(request: Request):
    return await _notify_only(request, "decision.duplicate",
                              "Duplicate submission; the original is being processed")


@app.post("/events/decision-human-approved", tags=["events"])
async def on_human_approved(request: Request):
    event = _extract_event(await request.json())
    cid = event.get("correlation_id")
    if not cid:
        return JSONResponse(content={"status": "DROP"})
    set_correlation_id(cid)
    await repo.set_queue_status(await get_pool(), cid, "approved")
    await _resume(cid, "approved", str(event.get("approver_email") or "unknown"))
    await _notify(
        cid,
        recipient=str(event.get("submitter_email") or ""),
        subject="Your submission was approved",
        body=str(event.get("reason") or ""),
        event_type="decision.human_approved",
    )
    return JSONResponse(content={"status": "SUCCESS"})


@app.post("/events/decision-human-rejected", tags=["events"])
async def on_human_rejected(request: Request):
    event = _extract_event(await request.json())
    cid = event.get("correlation_id")
    if not cid:
        return JSONResponse(content={"status": "DROP"})
    set_correlation_id(cid)
    await repo.set_queue_status(await get_pool(), cid, "rejected")
    await _resume(cid, "rejected", str(event.get("approver_email") or "unknown"))
    await _notify(
        cid,
        recipient=str(event.get("submitter_email") or ""),
        subject="Your submission was rejected",
        body=str(event.get("reason") or ""),
        event_type="decision.human_rejected",
    )
    return JSONResponse(content={"status": "SUCCESS"})


@app.post("/events/decision-info-requested", tags=["events"])
async def on_info_requested(request: Request):
    """Persist the approver's question and tell the submitter what is needed."""
    event = _extract_event(await request.json())
    cid = event.get("correlation_id")
    if not cid:
        return JSONResponse(content={"status": "DROP"})
    set_correlation_id(cid)

    requested_fields = list(event.get("requested_fields") or [])
    question = str(event.get("question") or event.get("approver_comment") or "")

    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await repo.create_info_request(
            conn,
            correlation_id=cid,
            revision=int(event.get("revision", 0) or 0),
            requested_by=str(event.get("approver_email") or "approver"),
            requested_fields=requested_fields,
            question=question,
        )
    await repo.set_queue_status(pool, cid, "info_requested")
    await _notify(
        cid,
        recipient=str(event.get("submitter_email") or ""),
        subject="More information needed for your submission",
        body=(
            f"{question}\nRequested fields: {', '.join(requested_fields) or 'see question'}"
        ),
        event_type="decision.info_requested",
    )
    logger.info("Information request recorded: %s", requested_fields or question)
    return JSONResponse(content={"status": "SUCCESS"})


def _normalise_exchange(raw: Any) -> list[dict[str, Any]]:
    """Coerce an information-exchange thread into a list of dicts.

    A consumer must not crash on a shape it did not expect: a handler that raises
    returns 500, Dapr redelivers, and the same event fails forever while filling
    the log. Anything that is not a usable entry is dropped with a warning, and
    entries that arrive as JSON text are parsed.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("info_exchange is not JSON, ignoring it")
            return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    entries: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except json.JSONDecodeError:
                logger.warning("Skipping an info_exchange entry that is not JSON")
                continue
        # A doubly-encoded thread arrives as a list holding one list.
        if isinstance(item, list):
            entries.extend(e for e in item if isinstance(e, dict))
        elif isinstance(item, dict):
            entries.append(item)
        else:
            logger.warning("Skipping an info_exchange entry of type %s", type(item).__name__)
    return entries


@app.post("/events/submission-info-provided", tags=["events"])
async def on_info_provided(request: Request):
    """Close the open request and attach the answer to the queue entry.

    The item goes back to ``pending`` only after re-analysis escalates it again,
    so an approver never sees a stale rationale, but the thread is stored now so
    it is visible the moment it returns.
    """
    event = _extract_event(await request.json())
    cid = event.get("correlation_id")
    if not cid:
        return JSONResponse(content={"status": "DROP"})
    set_correlation_id(cid)

    exchange = _normalise_exchange(event.get("info_exchange"))
    last = exchange[-1] if exchange else {}
    answer = last.get("answer", {})
    pool = await get_pool()
    await repo.close_info_request(
        pool,
        cid,
        answer=answer,
        answered_by=str(event.get("submitter_email") or "submitter"),
    )
    await repo.append_exchange(pool, cid, exchange)
    logger.info("Information request answered; awaiting re-analysis")
    return JSONResponse(content={"status": "SUCCESS"})


@app.post("/events/payment-completed", tags=["events"])
async def on_payment_completed(request: Request):
    return await _notify_only(request, "payment.completed", "Your expense was paid")


@app.post("/events/payment-failed", tags=["events"])
async def on_payment_failed(request: Request):
    return await _notify_only(request, "payment.failed",
                              "Payment could not be completed")


@app.post("/events/payment-compensated", tags=["events"])
async def on_payment_compensated(request: Request):
    return await _notify_only(
        request, "payment.compensated",
        "Payment failed and the budget reservation was released"
    )


async def _notify_only(request: Request, event_type: str, subject: str) -> JSONResponse:
    event = _extract_event(await request.json())
    cid = event.get("correlation_id")
    if not cid:
        return JSONResponse(content={"status": "DROP"})
    set_correlation_id(cid)
    await _notify(
        cid,
        recipient=str(event.get("submitter_email") or ""),
        subject=subject,
        body=str(event.get("reason") or event.get("error") or ""),
        event_type=event_type,
    )
    return JSONResponse(content={"status": "SUCCESS"})


async def _notify(
    correlation_id: str, *, recipient: str, subject: str, body: str, event_type: str
) -> None:
    """Deliver and record a notification (M8)."""
    logger.info(
        "NOTIFY %s: %s", recipient or "unknown", subject,
        extra={"correlation_id": correlation_id, "event_type": event_type},
    )
    try:
        await repo.record_notification(
            await get_pool(),
            correlation_id=correlation_id,
            recipient=recipient,
            subject=subject,
            body=body,
            event_type=event_type,
        )
    except Exception as exc:
        logger.error("Could not record notification: %s", exc)


# ── Approver API (F4 / F5) ──────────────────────────────────────────────────


@app.get("/api/approvals", tags=["approvals"])
async def list_approvals(
    status: str | None = Query(default="pending"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _p: Principal = Depends(require_roles(ROLE_APPROVER)),
):
    """The escalation queue, only what the system could not handle itself (F4/F6)."""
    items = await repo.list_queue(
        await get_pool(), status=status, limit=limit, offset=offset
    )
    return {"approvals": items, "count": len(items)}


def _require_approver_email(body: dict[str, Any]) -> str | None:
    """The trimmed ``approver_email`` from a request body, or ``None``.

    An approval has to be attributable. When a verified token is present its
    email is authoritative; when authentication is disabled for a demo the body
    is all there is, and a blank value must be refused rather than recorded as
    "unknown".
    """
    value = str(body.get("approver_email") or "").strip()
    return value or None


def _resolve_approver(action_email: str, principal: Principal) -> str:
    """Attribute the decision, preferring the verified token over the body.

    The anonymous principal that stands in when authentication is disabled is not
    an identity, so in that mode the body has to supply one. Recording a decision
    against "anonymous" would put a hole in the audit trail exactly where F9 wants
    a name.

    Raises:
        HTTPException: 400 when neither source yields an identity.
    """
    from approvalflow.auth import ANONYMOUS

    from_body = _require_approver_email({"approver_email": action_email})
    from_token = (principal.email or "").strip()
    if principal.subject == ANONYMOUS.subject:
        from_token = ""

    email = from_body or from_token
    if not email:
        raise HTTPException(
            status_code=400,
            detail="approver_email is required when there is no authenticated identity",
        )
    return email


class ApproverAction(BaseModel):
    """Approve or reject."""

    approver_email: str = Field(default="")
    comment: str = Field(default="")


class SendBackAction(BaseModel):
    """Send back for more information, the structured request (F5).

    ``requested_fields`` is what makes this actionable: the submitter's tracking
    screen renders a form for exactly these fields instead of a vague "more info
    needed", and the audit trail records what was asked.
    """

    approver_email: str = Field(default="")
    question: str = Field(
        min_length=3,
        description="What the approver needs from the submitter or vendor",
    )
    requested_fields: list[str] = Field(
        default_factory=list,
        description="Field names the submitter should supply, e.g. ['receiptPresent']",
    )


@app.post("/api/approvals/{correlation_id}/approve", tags=["approvals"])
async def approve(
    correlation_id: str,
    action: ApproverAction,
    principal: Principal = Depends(require_roles(ROLE_APPROVER)),
):
    """Approve an escalated item; the router publishes the final decision."""
    return await _record_action(
        correlation_id,
        action="approve",
        approver_email=_resolve_approver(action.approver_email, principal),
        comment=action.comment,
        next_status="approved",
    )


@app.post("/api/approvals/{correlation_id}/reject", tags=["approvals"])
async def reject(
    correlation_id: str,
    action: ApproverAction,
    principal: Principal = Depends(require_roles(ROLE_APPROVER)),
):
    """Reject an escalated item."""
    return await _record_action(
        correlation_id,
        action="reject",
        approver_email=_resolve_approver(action.approver_email, principal),
        comment=action.comment,
        next_status="rejected",
    )


@app.post("/api/approvals/{correlation_id}/send-back", tags=["approvals"])
async def send_back(
    correlation_id: str,
    action: SendBackAction,
    principal: Principal = Depends(require_roles(ROLE_APPROVER)),
):
    """Ask the submitter or vendor for specific missing information (F5).

    The workflow stays paused on the same correlation id; answering resumes it at
    the point it stopped rather than starting a new submission.
    """
    return await _record_action(
        correlation_id,
        action="send_back",
        approver_email=_resolve_approver(action.approver_email, principal),
        comment=action.question,
        next_status="info_requested",
        extra={
            "question": action.question,
            "requested_fields": action.requested_fields,
        },
    )


async def _record_action(
    correlation_id: str,
    *,
    action: str,
    approver_email: str,
    comment: str,
    next_status: str,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    """Persist an approver's action and enqueue the event in one transaction.

    The click is durable before anything is published, and the publish cannot be
    lost afterwards, the outbox owns it. The row is locked and status-checked, so
    a double click or two approvers acting at once produce exactly one decision.
    """
    set_correlation_id(correlation_id)
    pool = await get_pool()

    async with pool.acquire() as conn, conn.transaction():
        row = await repo.claim_for_action(
            conn, correlation_id, ("pending", "info_requested")
        )
        if row is None:
            current = await repo.get_queue_item(pool, correlation_id)
            if current is None:
                raise HTTPException(status_code=404, detail="Not in the approval queue")
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Item is '{current['status']}' and can no longer be actioned"
                ),
            )

        revision = int(row["revision"])
        await conn.execute(
            """
                UPDATE notification.approval_queue
                SET status = $2, approver_email = $3, approver_action = $4,
                    approver_comment = $5, resumed_at = NOW(), updated_at = NOW()
                WHERE correlation_id = $1::uuid
                """,
            correlation_id,
            next_status,
            approver_email or "unknown",
            action,
            comment,
        )

        payload: dict[str, Any] = {
            "correlation_id": correlation_id,
            "revision": revision,
            "action": action,
            "approver_email": approver_email or "unknown",
            "comment": comment,
            "submitter_email": row["submitter_email"],
            "department": row["department"],
        }
        if extra:
            payload.update(extra)

        await enqueue(
            conn,
            correlation_id=correlation_id,
            topic=APPROVAL_ACTION_RECEIVED,
            payload=payload,
            table=OUTBOX_TABLE,
        )

    await dispatcher.dispatch_once()
    logger.info("Approver action '%s' recorded by %s", action, approver_email)
    return JSONResponse(
        content={
            "status": "SUCCESS",
            "correlation_id": correlation_id,
            "action": action,
            "revision": revision,
        }
    )


# ── Internal API (Dapr service invocation, used by the audit trail) ─────────


@app.get("/internal/approvals/{correlation_id}", tags=["internal"])
async def internal_approval_record(correlation_id: str):
    """Everything the human-review side knows about one item (F9)."""
    pool = await get_pool()
    return {
        "queue_item": await repo.get_queue_item(pool, correlation_id),
        "info_requests": await repo.info_requests(pool, correlation_id),
        "notifications": await repo.notifications(pool, correlation_id),
        "pause_token": await state.get(_pause_key(correlation_id)),
    }


@app.get("/api/approvals/{correlation_id}", tags=["approvals"])
async def get_approval(
    correlation_id: str,
    _p: Principal = Depends(require_roles(ROLE_APPROVER, ROLE_ADMIN)),
):
    """One queue entry with its information-request thread."""
    pool = await get_pool()
    item = await repo.get_queue_item(pool, correlation_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Not in the approval queue")
    item["info_requests"] = await repo.info_requests(pool, correlation_id)
    return item
