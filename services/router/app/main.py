"""The deterministic decision authority.

Responsibilities:

* subscribe to agent.analyzed, run the gate chain, publish decision.*;
* subscribe to approval.action_received, validate a human's action and publish
  the resulting decision, so routes are decided in exactly one place;
* serve the policy configuration API (F7 / M13): read and update the live
  thresholds and rules with no redeploy;
* serve the controller reports: the dashboard (F8) and the ceiling proof (F10).

There is no LLM library in this service's requirements.txt, which is what makes
the M12 guarantee structural rather than a matter of care.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
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
from approvalflow.db import close_pool, get_pool, healthy, json_safe, wait_for_pool
from approvalflow.events import (
    DECISION_AUTO_APPROVED,
    DECISION_DUPLICATE,
    DECISION_ESCALATED,
    DECISION_HUMAN_APPROVED,
    DECISION_HUMAN_REJECTED,
    DECISION_INFO_REQUESTED,
    DECISION_REJECTED,
)
from approvalflow.logging import get_logger, set_correlation_id
from approvalflow.middleware import CorrelationIdMiddleware
from approvalflow.models import DecisionRoute
from approvalflow.outbox import DaprEventPublisher, OutboxDispatcher, enqueue
from approvalflow.policy import PolicyConfigError, PolicyConfigStore
from approvalflow.ruleengine import (
    RuleEvaluationError,
    rule_catalogue,
    validate_rules_syntax,
)

from . import repository as repo
from .decision import decide

logger = get_logger(__name__)

OUTBOX_TABLE = "router.outbox"

state = DaprStateClient()
policy_store = PolicyConfigStore(state)
publisher = DaprEventPublisher()
dispatcher = OutboxDispatcher(get_pool, publisher, table=OUTBOX_TABLE)

# Route → topic. Human-path routes are strings because they are not agent-facing
# DecisionRoute values.
TOPIC_MAP: dict[str, str] = {
    DecisionRoute.AUTO_APPROVE.value: DECISION_AUTO_APPROVED,
    DecisionRoute.HUMAN_REVIEW.value: DECISION_ESCALATED,
    DecisionRoute.REJECT.value: DECISION_REJECTED,
    DecisionRoute.DUPLICATE.value: DECISION_DUPLICATE,
}

#: Approver action → (event topic, recorded route).
APPROVAL_ACTIONS: dict[str, tuple[str, str]] = {
    "approve": (DECISION_HUMAN_APPROVED, "human_approved"),
    "reject": (DECISION_HUMAN_REJECTED, "human_rejected"),
    "send_back": (DECISION_INFO_REQUESTED, "info_requested"),
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Bring up the pool and the outbox dispatcher; tear them down cleanly."""
    log_auth_mode("router")
    await wait_for_pool()
    config = await policy_store.get()
    await repo.record_policy_version(
        await get_pool(), config.version, config.to_json_dict(), config.updated_by
    )
    logger.info(
        "Router ready, policy config v%s, ceilings=%s, confidence>=%s",
        config.version,
        {k: str(v) for k, v in config.autonomy.category_ceilings_usd.items()},
        config.autonomy.confidence_threshold,
    )
    dispatcher.start()
    try:
        yield
    finally:
        await dispatcher.stop()
        await close_pool()


app = FastAPI(
    title="ApprovalFlow Router Service",
    version="1.0.0",
    description=(
        "Deterministic decision authority. Applies the configured policy, the "
        "autonomy ceiling and the confidence bar. Contains no AI."
    ),
    lifespan=lifespan,
)
app.add_middleware(CorrelationIdMiddleware)


def _extract_event(body: dict[str, Any]) -> dict[str, Any]:
    """Unwrap Dapr's CloudEvent envelope (``{"data": {"data": {...}}}``)."""
    payload = body.get("data", body)
    return payload.get("data", payload) if isinstance(payload, dict) else body


@app.get("/health", tags=["ops"])
async def health():
    """Liveness plus the dependencies that make this service *useful*."""
    db_ok = await healthy()
    body = {
        "status": "ok" if db_ok else "degraded",
        "service": "router",
        "database": "ok" if db_ok else "unavailable",
    }
    return JSONResponse(status_code=200 if db_ok else 503, content=body)


# ── Decision path ───────────────────────────────────────────────────────────


@app.post("/events/agent-analyzed", tags=["events"])
async def handle_agent_analyzed(request: Request):
    """Decide an item after the agent has analysed it.

    The duplicate flag is *not* recomputed here: intake already established it
    transactionally, so there is exactly one authority for "is this a repeat".
    """
    event = _extract_event(await request.json())
    correlation_id = event.get("correlation_id")
    if not correlation_id:
        logger.error("agent.analyzed without a correlation_id; dropping")
        return JSONResponse(content={"status": "DROP", "reason": "missing correlation_id"})
    set_correlation_id(correlation_id)

    revision = int(event.get("revision", 0) or 0)
    is_duplicate = bool(event.get("is_duplicate") or event.get("duplicate_of"))

    config = await policy_store.get()
    decision = decide(
        agent_output=event,
        submission=event,
        existing_duplicate=is_duplicate,
        config=config,
    )
    route = decision["route"]

    decision_event = {
        "correlation_id": correlation_id,
        "revision": revision,
        "route": route.value,
        "reason": decision["reason"],
        "rule_ids": decision["rule_ids"],
        "decided_by": "router",
        "policy_config_version": decision["policy_config_version"],
        "ceiling_applied_usd": str(decision["ceiling_applied_usd"]),
        "confidence_threshold": str(decision["confidence_threshold"]),
        "amount_usd": str(decision["enforced_amount_usd"]),
        "category": decision["enforced_category"],
        "agent_recommendation": event.get("recommendation"),
        "agent_confidence": event.get("confidence"),
        "agent_reasoning": event.get("reasoning"),
        "retrieved_rule_ids": event.get("retrieved_rule_ids", []),
        "department": event.get("department", ""),
        "vendor": event.get("vendor", ""),
        "invoice_number": event.get("invoice_number", ""),
        "submitter_email": event.get("submitter_email", ""),
        "duplicate_of": event.get("duplicate_of"),
    }

    written = await _persist_and_enqueue(
        decision_record={
            "correlation_id": correlation_id,
            "revision": revision,
            "invoice_number": event.get("invoice_number", ""),
            "department": event.get("department", ""),
            "agent_recommendation": event.get("recommendation"),
            "agent_confidence": _dec(event.get("confidence")),
            "agent_violations": event.get("violations", []),
            "agent_reasoning": event.get("reasoning"),
            "agent_amount_usd": _dec(event.get("amount_usd")),
            "agent_category": event.get("category"),
            "retrieved_rule_ids": event.get("retrieved_rule_ids", []),
            "submitted_amount_usd": _dec(event.get("submitted_amount_usd")),
            "enforced_amount_usd": decision["enforced_amount_usd"],
            "enforced_category": decision["enforced_category"],
            "ceiling_applied_usd": decision["ceiling_applied_usd"],
            "confidence_threshold": decision["confidence_threshold"],
            "policy_config_version": decision["policy_config_version"],
            "rule_ids": decision["rule_ids"],
            "final_route": route.value,
            "decision_reason": decision["reason"],
            "decided_by": "router",
        },
        topic=TOPIC_MAP[route.value],
        event_payload=decision_event,
        correlation_id=correlation_id,
    )

    return JSONResponse(
        content={
            "status": "SUCCESS",
            "correlation_id": correlation_id,
            "route": route.value,
            "duplicate_delivery": not written,
        }
    )


@app.post("/events/approval-action-received", tags=["events"])
async def handle_approval_action(request: Request):
    """Validate a human action and publish the resulting decision.

    Guards:

    * unknown action → recorded and dropped, never silently treated as approval;
    * a second action for the same round is a no-op (M10), enforced by the unique
      constraint on ``(correlation_id, revision, decided_by)``.
    """
    event = _extract_event(await request.json())
    correlation_id = event.get("correlation_id")
    if not correlation_id:
        return JSONResponse(content={"status": "DROP", "reason": "missing correlation_id"})
    set_correlation_id(correlation_id)

    action = str(event.get("action", "")).strip()
    approver_email = str(event.get("approver_email") or "unknown")
    comment = str(event.get("comment") or "")

    if action not in APPROVAL_ACTIONS:
        logger.error("Rejected unknown approver action '%s'", action)
        return JSONResponse(
            content={"status": "REJECTED", "reason": f"unknown action '{action}'"}
        )

    pool = await get_pool()
    prior = await repo.latest_decision(pool, correlation_id)
    if prior is None:
        logger.error("Approver action for an item with no decision history")
        return JSONResponse(
            status_code=409,
            content={"status": "REJECTED", "reason": "no decision on record for this item"},
        )

    # Take the revision from our own decision history, not from the event: the
    # event carries whatever round the approver's client last saw, which goes
    # stale the moment a re-analysis creates a new round.
    revision = int(prior["revision"])

    # An approver may only act on a round that is open for them, which also keeps
    # an approve off an item that has already been decided.
    if prior["final_route"] != DecisionRoute.HUMAN_REVIEW.value:
        logger.info(
            "Approver action ignored: item is not awaiting a human (route=%s)",
            prior["final_route"],
        )
        return JSONResponse(
            status_code=409,
            content={
                "status": "REJECTED",
                "reason": f"item is not awaiting a human decision "
                f"(current route: {prior['final_route']})",
                "correlation_id": correlation_id,
            },
        )

    if await repo.human_decision_exists(pool, correlation_id, revision):
        logger.info("Human decision already recorded for revision %s, no-op", revision)
        return JSONResponse(
            status_code=409,
            content={"status": "REJECTED", "correlation_id": correlation_id, "duplicate": True},
        )

    topic, route = APPROVAL_ACTIONS[action]
    config = await policy_store.get()

    decision_event: dict[str, Any] = {
        "correlation_id": correlation_id,
        "revision": revision,
        "route": route,
        "reason": f"Human decision: {action} by {approver_email}"
        + (f", {comment}" if comment else ""),
        "rule_ids": [],
        "decided_by": approver_email,
        "approver_email": approver_email,
        "approver_comment": comment,
        "amount_usd": prior.get("enforced_amount_usd"),
        "category": prior.get("enforced_category"),
        # Taken from the recorded decision, not from the approver's request: the
        # payment service needs the department to reserve against and the invoice
        # number for its own idempotency, and neither may be influenced by
        # whatever the approver's client happened to send.
        "department": prior.get("department") or event.get("department", ""),
        "invoice_number": prior.get("invoice_number", ""),
        "policy_config_version": config.version,
    }
    if action == "send_back":
        # Carry the actual request through, so the submitter is told *what* is
        # missing instead of just "more info needed" (F5).
        decision_event["requested_fields"] = event.get("requested_fields", [])
        decision_event["question"] = event.get("question", comment)

    written = await _persist_and_enqueue(
        decision_record={
            "correlation_id": correlation_id,
            "revision": revision,
            "invoice_number": prior.get("invoice_number"),
            "department": prior.get("department") or event.get("department"),
            "agent_recommendation": prior.get("agent_recommendation"),
            "agent_confidence": _dec(prior.get("agent_confidence")),
            "agent_violations": [],
            "agent_reasoning": prior.get("agent_reasoning"),
            "agent_amount_usd": _dec(prior.get("agent_amount_usd")),
            "agent_category": prior.get("agent_category"),
            "retrieved_rule_ids": prior.get("retrieved_rule_ids", []),
            "submitted_amount_usd": _dec(prior.get("submitted_amount_usd")),
            "enforced_amount_usd": _dec(prior.get("enforced_amount_usd")) or Decimal("0"),
            "enforced_category": prior.get("enforced_category") or "other",
            "ceiling_applied_usd": _dec(prior.get("ceiling_applied_usd")) or Decimal("0"),
            "confidence_threshold": _dec(prior.get("confidence_threshold")) or Decimal("0"),
            "policy_config_version": config.version,
            "rule_ids": [],
            "final_route": route,
            "decision_reason": decision_event["reason"],
            "decided_by": approver_email,
        },
        topic=topic,
        event_payload=decision_event,
        correlation_id=correlation_id,
    )

    if not written:
        # The row already existed, so nothing was published. Saying SUCCESS here
        # would tell the caller their decision took effect when it did not.
        logger.info("Human action was a redelivery, nothing published")
        return JSONResponse(
            content={
                "status": "SUCCESS",
                "correlation_id": correlation_id,
                "action": action,
                "route": route,
                "duplicate_delivery": True,
            }
        )

    logger.info(
        "Human action recorded", extra={"route": route, "correlation_id": correlation_id}
    )
    return JSONResponse(
        content={
            "status": "SUCCESS",
            "correlation_id": correlation_id,
            "action": action,
            "route": route,
            "duplicate_delivery": False,
        }
    )


async def _persist_and_enqueue(
    *,
    decision_record: dict[str, Any],
    topic: str,
    event_payload: dict[str, Any],
    correlation_id: str,
) -> bool:
    """Write the decision row and its outbound event in ONE transaction.

    Returns ``False`` when the round was already recorded (redelivered event), in
    which case nothing is enqueued a second time.
    """
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        inserted = await conn.fetchval(
            """
                INSERT INTO router.decisions (
                    correlation_id, revision, invoice_number, department,
                    agent_recommendation, agent_confidence, agent_violations,
                    agent_reasoning, agent_amount_usd, agent_category,
                    retrieved_rule_ids, submitted_amount_usd, enforced_amount_usd,
                    enforced_category, ceiling_applied_usd, confidence_threshold,
                    policy_config_version, rule_ids_applied, final_route,
                    decision_reason, decided_by
                ) VALUES (
                    $1::uuid, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11::text[],
                    $12, $13, $14, $15, $16, $17, $18::text[], $19, $20, $21
                )
                ON CONFLICT (correlation_id, revision, decided_by) DO NOTHING
                RETURNING id
                """,
            decision_record["correlation_id"],
            decision_record["revision"],
            decision_record.get("invoice_number"),
            decision_record.get("department"),
            decision_record.get("agent_recommendation"),
            decision_record.get("agent_confidence"),
            json_safe(decision_record.get("agent_violations", [])),
            decision_record.get("agent_reasoning"),
            decision_record.get("agent_amount_usd"),
            decision_record.get("agent_category"),
            list(decision_record.get("retrieved_rule_ids") or []),
            decision_record.get("submitted_amount_usd"),
            decision_record["enforced_amount_usd"],
            decision_record["enforced_category"],
            decision_record["ceiling_applied_usd"],
            decision_record["confidence_threshold"],
            decision_record["policy_config_version"],
            list(decision_record.get("rule_ids") or []),
            decision_record["final_route"],
            decision_record.get("decision_reason"),
            decision_record["decided_by"],
        )
        if inserted is None:
            logger.info("Decision round already recorded, skipping publish")
            return False
        await enqueue(
            conn,
            correlation_id=correlation_id,
            topic=topic,
            payload=event_payload,
            table=OUTBOX_TABLE,
        )
    # Publish immediately rather than waiting for the next poll; the dispatcher
    # is the safety net, not the happy path.
    await dispatcher.dispatch_once()
    return True


# ── Internal API (Dapr service invocation, the M5 synchronous path) ────────


@app.get("/internal/decisions/{correlation_id}", tags=["internal"])
async def internal_decision_trail(correlation_id: str):
    """Decision history for one item, called by the audit endpoint via Dapr."""
    pool = await get_pool()
    return {
        "correlation_id": correlation_id,
        "decisions": await repo.decision_trail(pool, correlation_id),
    }


# ── Policy configuration API (F7 / M13) ─────────────────────────────────────


class PolicyUpdate(BaseModel):
    """A full replacement policy document.

    Sending the whole document (rather than a patch) keeps the change atomic and
    reviewable: what you PUT is exactly what the router will enforce.
    """

    document: dict[str, Any] = Field(
        description="Complete policy document; see config/policy-config.json"
    )


class PolicyDocumentUpdate(BaseModel):
    """New policy prose for the agent to reason over."""

    text: str = Field(min_length=1, description="Markdown policy document")


@app.get("/api/admin/policy", tags=["admin"])
async def get_policy(_p: Principal = Depends(require_roles(ROLE_ADMIN))):
    """The live configuration, plus the bounds configuration cannot cross."""
    from approvalflow.policy import ABSOLUTE_MAX_CEILING_USD, MIN_CONFIDENCE_THRESHOLD

    config = await policy_store.get()
    return {
        "config": config.to_json_dict(),
        "rules": rule_catalogue(config),
        "hard_limits": {
            "absolute_max_ceiling_usd": str(ABSOLUTE_MAX_CEILING_USD),
            "min_confidence_threshold": str(MIN_CONFIDENCE_THRESHOLD),
            "note": (
                "Compiled into the router. A configuration that crosses these is "
                "rejected, and no configuration can express 'auto_approve'."
            ),
        },
    }


@app.put("/api/admin/policy", tags=["admin"])
async def update_policy(
    update: PolicyUpdate,
    principal: Principal = Depends(require_roles(ROLE_ADMIN)),
):
    """Replace the live policy configuration, no redeploy, no restart (F7/M13).

    Validation happens before anything is stored: schema, safety bounds, and a
    dry run of every rule predicate. An invalid document is refused with the
    reason, so a controller cannot accidentally disable the guardrails or leave
    the router escalating everything because of a typo.
    """
    try:
        candidate = update.document
        # Dry-run the rules first so a bad predicate never reaches the store.
        from approvalflow.policy import parse_policy_config

        probe = parse_policy_config({**candidate, "version": 0})
        validate_rules_syntax(probe)
        config = await policy_store.save(candidate, updated_by=principal.email or principal.subject)
    except (PolicyConfigError, RuleEvaluationError) as exc:
        logger.warning("Rejected policy update from %s: %s", principal.subject, exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    await repo.record_policy_version(
        await get_pool(), config.version, config.to_json_dict(), config.updated_by
    )
    logger.info(
        "Policy configuration is now v%s (by %s)", config.version, config.updated_by
    )
    return {
        "status": "updated",
        "version": config.version,
        "effective_within_seconds": 5,
        "config": config.to_json_dict(),
    }


@app.get("/api/admin/policy/versions", tags=["admin"])
async def policy_versions(_p: Principal = Depends(require_roles(ROLE_ADMIN))):
    """Who changed the autonomy posture, and when."""
    return {"versions": await repo.list_policy_versions(await get_pool())}


@app.get("/api/admin/policy-document", tags=["admin"])
async def get_policy_document(_p: Principal = Depends(require_roles(ROLE_ADMIN))):
    """The policy prose the agent retrieves clauses from."""
    text, version = await policy_store.get_document()
    return {"version": version, "text": text}


@app.put("/api/admin/policy-document", tags=["admin"])
async def update_policy_document(
    update: PolicyDocumentUpdate,
    principal: Principal = Depends(require_roles(ROLE_ADMIN)),
):
    """Replace the policy prose; the agent re-indexes it for retrieval."""
    try:
        version = await policy_store.save_document(
            update.text, updated_by=principal.email or principal.subject
        )
    except PolicyConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "updated", "version": version}


# ── Controller reports (F8 / F10) ───────────────────────────────────────────


@app.get("/api/reports/dashboard", tags=["reports"])
async def reports_dashboard(
    window_hours: int = Query(default=24, ge=1, le=720),
    _p: Principal = Depends(require_roles(ROLE_ADMIN, ROLE_APPROVER)),
):
    """Throughput, auto-approval vs escalation, and money split (F8)."""
    config = await policy_store.get()
    data = await repo.dashboard(await get_pool(), window_hours)
    data["policy"] = {
        "version": config.version,
        "confidence_threshold": str(config.autonomy.confidence_threshold),
        "ceilings_usd": {
            k: str(v) for k, v in config.autonomy.category_ceilings_usd.items()
        },
        "default_ceiling_usd": str(config.autonomy.default_ceiling_usd),
    }
    data["outbox"] = await dispatcher.stats()
    return data


@app.get("/api/reports/ceiling-proof", tags=["reports"])
async def reports_ceiling_proof(
    _p: Principal = Depends(require_roles(ROLE_ADMIN, ROLE_APPROVER)),
):
    """Evidence the system never auto-approved above the configured limit (F10).

    Returns ``holds: true`` only when *every* autonomous approval on record sat
    at or below the ceiling in force when it was made, and no autonomous approval
    was recorded by anything other than the router.
    """
    proof = await repo.ceiling_proof(await get_pool())
    proof["method"] = (
        "Every auto_approve row in router.decisions is compared against the "
        "ceiling and confidence bar stored on that row at decision time. The "
        "router has no LLM dependency, so no model output can reach this gate."
    )
    return proof


def _dec(value: Any) -> Decimal | None:
    """Coerce to ``Decimal`` for asyncpg numeric parameters.

    Values reach us as floats (events), strings (state store / previous rows) or
    ``Decimal`` (the decision itself); PostgreSQL numeric columns want exactly
    one of those, so normalise here rather than at nineteen call sites.
    """
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        logger.warning("Dropping unparseable numeric value %r", value)
        return None
