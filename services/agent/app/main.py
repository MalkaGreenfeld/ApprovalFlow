"""Agent Service, analyses submissions and recommends an outcome.

Subscribes to ``submission.received`` and to ``submission.info_provided`` (the
re-analysis that follows a send-back), and publishes ``agent.analyzed``.

The agent has authority over nothing. What it produces is a recommendation, a
confidence, cited violations and the clause ids it was shown. The router decides.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from approvalflow.auth import ROLE_ADMIN, Principal, log_auth_mode, require_roles
from approvalflow.dapr_client import DaprStateClient
from approvalflow.events import AGENT_ANALYZED
from approvalflow.logging import get_logger, set_correlation_id
from approvalflow.middleware import CorrelationIdMiddleware
from approvalflow.policy import PolicyConfigStore

from .analyzer import analyze_submission
from .llm import configured_provider_name, get_provider, reset_provider
from .retrieval import get_index, reset_index

logger = get_logger(__name__)

DAPR_HTTP_PORT = os.getenv("DAPR_HTTP_PORT", "3500")
DAPR_PUBSUB_NAME = "pubsub"

state = DaprStateClient()
policy_store = PolicyConfigStore(state)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Warm the provider and the policy index so the first invoice is not slowest.

    A provider misconfiguration surfaces here, at startup, instead of on the first
    real submission.
    """
    log_auth_mode("agent")
    try:
        provider = await get_provider()
        logger.info("LLM provider ready: %s (%s)", configured_provider_name(), provider.model)
    except Exception as exc:
        # Not fatal: the service stays up and escalates every item to a human,
        # which is the safe failure mode. It is logged loudly and visible on /health.
        logger.error("LLM provider unavailable at startup: %s", exc)

    try:
        text, version = await policy_store.get_document()
        get_index(text, version)
    except Exception as exc:
        logger.error("Could not build the policy index at startup: %s", exc)
    yield


app = FastAPI(
    title="ApprovalFlow Agent Service",
    version="1.0.0",
    description="Policy-grounded analysis. Recommends; never decides.",
    lifespan=lifespan,
)
app.add_middleware(CorrelationIdMiddleware)


def _dapr_publish_url(topic: str) -> str:
    return f"http://localhost:{DAPR_HTTP_PORT}/v1.0/publish/{DAPR_PUBSUB_NAME}/{topic}"


def _extract_event(body: dict[str, Any]) -> dict[str, Any]:
    payload = body.get("data", body)
    return payload.get("data", payload) if isinstance(payload, dict) else body


@app.get("/health", tags=["ops"])
async def health():
    """Liveness, plus which provider and policy version are in force."""
    provider_ok = True
    model = None
    try:
        provider = await get_provider()
        model = provider.model
    except Exception as exc:
        provider_ok = False
        logger.warning("Health check: provider unavailable (%s)", exc)

    return {
        "status": "ok",
        "service": "agent",
        "llm_provider": configured_provider_name(),
        "llm_model": model,
        "llm_available": provider_ok,
    }


@app.post("/internal/reindex", tags=["internal"])
async def reindex(_p: Principal = Depends(require_roles(ROLE_ADMIN))):
    """Rebuild the retrieval index after a policy-document change (F7)."""
    reset_index()
    reset_provider()
    text, version = await policy_store.get_document()
    index = get_index(text, version)
    return {
        "status": "reindexed",
        "policy_version": version,
        "chunks": len(index.chunks),
        "rule_clauses": sum(1 for c in index.chunks if c.rule_id),
    }


@app.post("/events/submission-received", tags=["events"])
async def on_submission_received(request: Request):
    """Analyse a new submission."""
    return await _analyze_and_publish(await request.json(), reason="submission.received")


@app.post("/events/submission-info-provided", tags=["events"])
async def on_info_provided(request: Request):
    """Re-analyse after a send-back was answered (F5 resume path).

    Same code path as a first analysis, with the question/answer thread included
    in the prompt so the model judges the *complete* picture rather than the
    original gap.
    """
    return await _analyze_and_publish(
        await request.json(), reason="submission.info_provided"
    )


async def _analyze_and_publish(body: dict[str, Any], *, reason: str) -> JSONResponse:
    event = _extract_event(body)
    correlation_id = event.get("correlation_id")
    if not correlation_id:
        logger.error("%s without a correlation_id; dropping", reason)
        return JSONResponse(content={"status": "DROP"})
    set_correlation_id(correlation_id)

    # A duplicate needs no analysis: the router will short-circuit it, and calling
    # a model for an invoice we already processed is waste (and cost).
    if event.get("is_duplicate"):
        logger.info("Skipping analysis for a known duplicate")
        return await _publish_analyzed(
            correlation_id,
            event,
            recommendation="human_review",
            confidence=0.0,
            violations=[{"rule_id": "GLOBAL-DUP", "description": "Duplicate submission"}],
            reasoning="Duplicate of an in-flight submission; no analysis performed.",
            amount_usd=float(str(event.get("amount_usd", 0) or 0)),
            category=str(event.get("category", "other")),
            retrieved=["GLOBAL-DUP"],
            policy_version=0,
        )

    policy_text, policy_version = await policy_store.get_document()
    result = await analyze_submission(event, policy_text, policy_version)
    output = result.output

    logger.info(
        "Analysis complete: %s (confidence %.2f)%s",
        output.recommendation.value,
        output.confidence,
        " [FAILED, escalating]" if result.failed else "",
        extra={"amount_usd": float(output.amount_usd)},
    )

    return await _publish_analyzed(
        correlation_id,
        event,
        recommendation=output.recommendation.value,
        confidence=output.confidence,
        violations=[
            {"rule_id": v.rule_id, "description": v.description} for v in output.violations
        ],
        reasoning=output.reasoning,
        amount_usd=float(output.amount_usd),
        category=output.category,
        retrieved=result.retrieved_rule_ids,
        policy_version=result.policy_version,
        analysis_failed=result.failed,
        analysis_error=result.error,
        model=result.model,
    )


async def _publish_analyzed(
    correlation_id: str,
    event: dict[str, Any],
    *,
    recommendation: str,
    confidence: float,
    violations: list[dict[str, str]],
    reasoning: str,
    amount_usd: float,
    category: str,
    retrieved: list[str],
    policy_version: int,
    analysis_failed: bool = False,
    analysis_error: str | None = None,
    model: str | None = None,
) -> JSONResponse:
    """Publish ``agent.analyzed``, forwarding the intake facts the router needs.

    The submitted amount and category are passed through **unchanged** alongside
    the agent's restatement, which is what lets the router enforce against the
    stricter of the two.
    """
    analyzed = {
        "correlation_id": correlation_id,
        "revision": int(event.get("revision", 0) or 0),
        # Advisory analysis
        "recommendation": recommendation,
        "confidence": confidence,
        "violations": violations,
        "reasoning": reasoning,
        "amount_usd": amount_usd,
        "category": category,
        "retrieved_rule_ids": retrieved,
        "policy_document_version": policy_version,
        "analysis_failed": analysis_failed,
        "analysis_error": analysis_error,
        "model": model,
        # Intake facts, forwarded verbatim
        "submitted_amount_usd": event.get("submitted_amount_usd", event.get("amount_usd")),
        "submitted_category": event.get("submitted_category", event.get("category")),
        "vendor": event.get("vendor", ""),
        "vendor_known": event.get("vendor_known", False),
        "receipt_present": event.get("receipt_present", False),
        "math_ok": event.get("math_ok", True),
        "currency": event.get("currency", "USD"),
        "line_items": event.get("line_items", []),
        "attendees": event.get("attendees"),
        "notes": event.get("notes", ""),
        "invoice_number": event.get("invoice_number", ""),
        "total": event.get("total", 0),
        "tax_amount": event.get("tax_amount", 0),
        "department": event.get("department", ""),
        "submitter_email": event.get("submitter_email", ""),
        "duplicate_of": event.get("duplicate_of"),
        "is_duplicate": bool(event.get("is_duplicate")),
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as client:
        try:
            resp = await client.post(_dapr_publish_url(AGENT_ANALYZED), json=analyzed)
        except Exception as exc:
            logger.exception("Failed to publish agent.analyzed: %s", exc)
            # Returning 500 makes Dapr redeliver; analysis is idempotent because
            # the router deduplicates by (correlation_id, revision).
            return JSONResponse(
                status_code=500,
                content={"status": "RETRY", "correlation_id": correlation_id},
            )

    if resp.status_code not in (200, 204):
        logger.error("Dapr rejected agent.analyzed: %s %s", resp.status_code, resp.text[:200])
        return JSONResponse(
            status_code=500, content={"status": "RETRY", "correlation_id": correlation_id}
        )

    return JSONResponse(content={"status": "SUCCESS", "correlation_id": correlation_id})
