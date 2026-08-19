"""The analysis step: retrieve the relevant policy, ask the model, validate.

Failure behaviour is deliberate (M15): any provider or validation error produces a
conservative ``human_review`` recommendation with confidence 0 and the rule id
``AGENT-ERROR``, and it is logged at error level. The system never silently
approves because the model was unavailable, and it never silently swallows the
problem either: the reason travels through to the audit trail.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from approvalflow.logging import get_logger
from approvalflow.models import AgentOutput, DecisionRoute, PolicyViolation

from .llm import LLMError, get_provider
from .prompts import INFO_EXCHANGE_BLOCK, SYSTEM_PROMPT, USER_PROMPT
from .retrieval import build_query, format_clauses, get_index, retrieved_rule_ids

logger = get_logger(__name__)

#: Strict JSON schema, the provider validates the shape so we never parse hope.
AGENT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "name": "expense_analysis",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "recommendation",
            "confidence",
            "violations",
            "reasoning",
            "amount_usd",
            "category",
        ],
        "properties": {
            "recommendation": {
                "type": "string",
                "enum": ["auto_approve", "human_review", "reject"],
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "violations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["rule_id", "description"],
                    "properties": {
                        "rule_id": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
            },
            "reasoning": {"type": "string"},
            "amount_usd": {"type": "number"},
            "category": {
                "type": "string",
                "enum": ["meals", "travel", "saas", "hardware", "other"],
            },
        },
    },
}


class AnalysisResult:
    """The agent's advisory output plus the retrieval evidence behind it."""

    def __init__(
        self,
        output: AgentOutput,
        *,
        retrieved_rule_ids: list[str],
        policy_version: int,
        provider: str,
        model: str,
        failed: bool = False,
        error: str | None = None,
    ) -> None:
        self.output = output
        self.retrieved_rule_ids = retrieved_rule_ids
        self.policy_version = policy_version
        self.provider = provider
        self.model = model
        self.failed = failed
        self.error = error


def _conservative_output(amount_usd: Any, category: Any, reason: str) -> AgentOutput:
    """The output used whenever we cannot trust an analysis."""
    return AgentOutput(
        recommendation=DecisionRoute.HUMAN_REVIEW,
        confidence=0.0,
        violations=[PolicyViolation(rule_id="AGENT-ERROR", description=reason)],
        reasoning=f"Automated analysis unavailable: {reason}. Escalated for a human.",
        amount_usd=Decimal(str(amount_usd or 0)),
        category=str(category or "other"),
    )


async def analyze_submission(
    event_data: dict[str, Any], policy_text: str, policy_version: int
) -> AnalysisResult:
    """Retrieve relevant clauses, ask the provider, and validate the answer.

    Args:
        event_data: The submission event (or the amended one after a send-back).
        policy_text: Current policy prose, from the live configuration store.
        policy_version: Its version, recorded for the audit trail.

    Returns:
        An :class:`AnalysisResult`. It always returns, errors become a
        conservative escalation rather than an exception, because dropping the
        event would leave the submitter with a tracking id and no outcome.
    """
    index = get_index(policy_text, policy_version)
    category = str(event_data.get("category", "other") or "other").lower()
    results = index.retrieve(build_query(event_data), category=category)
    clause_ids = retrieved_rule_ids(results)

    logger.info(
        "Retrieved %s policy clauses for analysis (%s)",
        len(results),
        ", ".join(clause_ids) or "prose only",
    )

    exchange = event_data.get("info_exchange") or []
    exchange_block = (
        INFO_EXCHANGE_BLOCK.format(exchange=json.dumps(exchange, ensure_ascii=False, indent=2))
        if exchange
        else ""
    )

    user_prompt = USER_PROMPT.format(
        submitter=event_data.get("submitter_email", ""),
        department=event_data.get("department", ""),
        vendor=event_data.get("vendor", ""),
        vendor_known=event_data.get("vendor_known", False),
        invoice_number=event_data.get("invoice_number", ""),
        currency=event_data.get("currency", "USD"),
        category=category,
        attendees=event_data.get("attendees") if event_data.get("attendees") else "not provided",
        line_items=json.dumps(event_data.get("line_items", []), ensure_ascii=False),
        tax_amount=event_data.get("tax_amount", 0),
        total=event_data.get("total", 0),
        amount_usd=event_data.get("amount_usd", 0),
        receipt_present=event_data.get("receipt_present", False),
        math_ok=event_data.get("math_ok", True),
        date=event_data.get("date") or "not provided",
        revision=event_data.get("revision", 0),
        notes=event_data.get("notes") or "(none)",
        info_exchange_block=exchange_block,
    )

    provider_name = "unknown"
    model_name = "unknown"
    try:
        provider = await get_provider()
        provider_name = type(provider).__name__
        model_name = provider.model
        raw = await provider.complete_json(
            instructions=SYSTEM_PROMPT.format(policy_clauses=format_clauses(results)),
            user_input=user_prompt,
            schema=AGENT_OUTPUT_SCHEMA,
        )
        output = _validate(raw, event_data, clause_ids)
    except LLMError as exc:
        logger.error("LLM provider failure: %s", exc)
        return AnalysisResult(
            _conservative_output(event_data.get("amount_usd"), category, str(exc)),
            retrieved_rule_ids=clause_ids,
            policy_version=policy_version,
            provider=provider_name,
            model=model_name,
            failed=True,
            error=str(exc),
        )
    except (ValueError, TypeError, KeyError) as exc:
        logger.exception("Agent output failed validation: %s", exc)
        return AnalysisResult(
            _conservative_output(
                event_data.get("amount_usd"), category, f"invalid model output: {exc}"
            ),
            retrieved_rule_ids=clause_ids,
            policy_version=policy_version,
            provider=provider_name,
            model=model_name,
            failed=True,
            error=str(exc),
        )

    return AnalysisResult(
        output,
        retrieved_rule_ids=clause_ids,
        policy_version=policy_version,
        provider=provider_name,
        model=model_name,
    )


def _validate(
    raw: dict[str, Any], event_data: dict[str, Any], clause_ids: list[str]
) -> AgentOutput:
    """Turn the model's JSON into a typed output, rejecting invented rule ids.

    A model citing a rule it was never shown is a hallucination; keeping it would
    put a fictional clause into the audit trail an approver relies on. Such
    citations are dropped and logged rather than silently trusted.
    """
    recommendation = DecisionRoute(str(raw.get("recommendation", "human_review")))
    confidence = float(raw.get("confidence", 0.0))
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence {confidence} outside [0, 1]")

    violations: list[PolicyViolation] = []
    for entry in raw.get("violations", []) or []:
        rule_id = str(entry.get("rule_id", "")).strip().upper()
        if clause_ids and rule_id not in clause_ids:
            logger.warning(
                "Discarding citation of %s, it was not among the retrieved clauses",
                rule_id,
            )
            continue
        violations.append(
            PolicyViolation(
                rule_id=rule_id,
                description=str(entry.get("description", ""))[:500],
            )
        )

    return AgentOutput(
        recommendation=recommendation,
        confidence=confidence,
        violations=violations,
        reasoning=str(raw.get("reasoning", ""))[:2000],
        amount_usd=Decimal(str(raw.get("amount_usd", event_data.get("amount_usd", 0)))),
        category=str(raw.get("category", event_data.get("category", "other"))).lower(),
    )
