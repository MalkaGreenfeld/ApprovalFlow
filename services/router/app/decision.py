"""Deterministic decision gates (M12).

This is a zero-AI code path: the router has no LLM library in its requirements,
so it cannot call a model. The agent recommends; the numbers here decide.

The thresholds and the rule catalogue are data, loaded from the live policy
configuration (approvalflow.policy) and editable through PUT /api/admin/policy.
The gates themselves are code: configuration can express "escalate" or "reject",
never "auto-approve".

Two properties keep the agent from widening its own limits:

1. the ceiling is applied to max(submitted amount, agent amount), so a lower
   reported amount_usd buys nothing;
2. the ceiling uses whichever of the two categories is stricter, and the
   per-category rules of both are evaluated, so a re-label sheds no cap.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from approvalflow.facts import build_facts, to_decimal
from approvalflow.logging import get_logger
from approvalflow.models import DecisionRoute
from approvalflow.policy import PolicyConfig, RuleOutcome, default_policy_config
from approvalflow.ruleengine import (
    RuleEvaluationError,
    evaluate_rules,
    most_severe,
)

logger = get_logger(__name__)

#: Rule ids emitted by the gates that live in code rather than in the config.
CEILING_RULE_ID = "AUTONOMY-CEILING"
CONFIDENCE_RULE_ID = "AUTONOMY-CONFIDENCE"
ENGINE_ERROR_RULE_ID = "POLICY-ENGINE-ERROR"

_OUTCOME_TO_ROUTE: dict[RuleOutcome, DecisionRoute] = {
    RuleOutcome.DUPLICATE: DecisionRoute.DUPLICATE,
    RuleOutcome.REJECT: DecisionRoute.REJECT,
    RuleOutcome.HUMAN_REVIEW: DecisionRoute.HUMAN_REVIEW,
}


def resolve_enforced_amount(
    submission: dict[str, Any], agent_output: dict[str, Any]
) -> Decimal:
    """The amount the ceiling is applied to: the larger of the two figures.

    The agent restates ``amount_usd`` in its structured output. Trusting that
    number would hand a model (or a prompt injection) a way to slip a $5,000
    invoice under a $750 ceiling by calling it $500.
    """
    submitted = to_decimal(
        submission.get("submitted_amount_usd", submission.get("amount_usd", 0))
    )
    from_agent = to_decimal(agent_output.get("amount_usd", 0))
    return max(submitted, from_agent)


def resolve_enforced_category(
    submission: dict[str, Any], agent_output: dict[str, Any], config: PolicyConfig
) -> str:
    """The category with the lower ceiling of (what was submitted, what the agent says).

    The agent is allowed to *correct* a category, that is useful, but only in
    the conservative direction as far as autonomy is concerned.
    """
    submitted = str(
        submission.get("submitted_category") or submission.get("category") or "other"
    ).strip().lower()
    from_agent = str(agent_output.get("category") or submitted).strip().lower()
    if from_agent == submitted:
        return submitted
    return min(
        (submitted, from_agent), key=lambda c: config.autonomy.ceiling_for(c)
    )


def resolve_candidate_categories(
    submission: dict[str, Any], agent_output: dict[str, Any]
) -> list[str]:
    """Every category whose rules are in scope for this item.

    Picking one category would let a re-label dodge a cap: an invoice submitted
    as ``saas`` at $220 (over the SaaS cap) called ``other`` by the agent would
    face no per-category rule at all. Evaluating the union costs nothing and can
    only ever escalate, never approve.
    """
    submitted = str(
        submission.get("submitted_category") or submission.get("category") or "other"
    ).strip().lower()
    from_agent = str(agent_output.get("category") or submitted).strip().lower()
    return [submitted] if from_agent == submitted else [submitted, from_agent]


def decide(
    agent_output: dict[str, Any],
    submission: dict[str, Any],
    existing_duplicate: bool = False,
    config: PolicyConfig | None = None,
) -> dict[str, Any]:
    """Run the deterministic gate chain.

    Args:
        agent_output: The agent's advisory analysis (recommendation, confidence,
            restated amount/category). Never trusted for authority.
        submission: The submission facts as recorded at intake.
        existing_duplicate: Authoritative duplicate flag from the transactional
            intake check.
        config: Policy configuration. Defaults to the bootstrap document so unit
            tests and offline runs need no sidecar.

    Returns:
        A dict with ``route``, ``reason``, ``rule_ids``, ``decided_by`` plus the
        enforcement metadata persisted for the audit trail and the ceiling proof:
        ``enforced_amount_usd``, ``enforced_category``, ``ceiling_applied_usd``,
        ``confidence_threshold``, ``policy_config_version``, ``matched_rule_ids``.
    """
    config = config or default_policy_config()

    enforced_amount = resolve_enforced_amount(submission, agent_output)
    enforced_category = resolve_enforced_category(submission, agent_output, config)
    candidate_categories = resolve_candidate_categories(submission, agent_output)
    ceiling = config.autonomy.ceiling_for(enforced_category)
    confidence_bar = config.autonomy.confidence_threshold
    confidence = to_decimal(agent_output.get("confidence", 0))

    def result(
        route: DecisionRoute, reason: str, rule_ids: list[str], decided_by: str = "router"
    ) -> dict[str, Any]:
        return {
            "route": route,
            "reason": reason,
            "rule_ids": rule_ids,
            "decided_by": decided_by,
            "enforced_amount_usd": enforced_amount,
            "enforced_category": enforced_category,
            "evaluated_categories": candidate_categories,
            "ceiling_applied_usd": ceiling,
            "confidence_threshold": confidence_bar,
            "policy_config_version": config.version,
            "matched_rule_ids": rule_ids,
        }

    facts = build_facts(
        submission,
        config=config,
        is_duplicate=existing_duplicate,
        amount_usd=enforced_amount,
        category=enforced_category,
    )
    facts["confidence"] = confidence

    # ── Gate 1: the configured rule catalogue ───────────────────────────────
    try:
        matches = evaluate_rules(config, facts, categories=candidate_categories)
    except RuleEvaluationError as exc:
        # A broken policy document must never read as "nothing to see here".
        # Fail in the safe direction: a human looks at it.
        logger.error(
            "Policy rule evaluation failed (%s), escalating to a human", exc
        )
        return result(
            DecisionRoute.HUMAN_REVIEW,
            f"Policy configuration error, escalated for safety: {exc}",
            [ENGINE_ERROR_RULE_ID],
        )

    deciding = most_severe(matches)
    if deciding is not None:
        cited: list[str] = []
        for match in [deciding, *[m for m in matches if m is not deciding]]:
            for rule_id in match.cited_rule_ids:
                if rule_id not in cited:
                    cited.append(rule_id)
        route = _OUTCOME_TO_ROUTE[deciding.outcome]
        logger.info(
            "Decision: %s (%s)", route.value, ", ".join(cited),
            extra={"route": route.value, "amount_usd": float(enforced_amount)},
        )
        return result(route, deciding.reason, cited)

    # ── Gate 2: the autonomy ceiling (unbypassable) ─────────────────────────
    if enforced_amount > ceiling:
        logger.info(
            "Decision: human_review (ceiling $%s > $%s for %s)",
            enforced_amount, ceiling, enforced_category,
            extra={"route": "human_review", "amount_usd": float(enforced_amount)},
        )
        return result(
            DecisionRoute.HUMAN_REVIEW,
            f"${enforced_amount} exceeds the ${ceiling} autonomy ceiling "
            f"for {enforced_category}",
            [CEILING_RULE_ID],
        )

    # ── Gate 3: the confidence bar ──────────────────────────────────────────
    if confidence < confidence_bar:
        logger.info(
            "Decision: human_review (confidence %s < %s)", confidence, confidence_bar,
            extra={"route": "human_review"},
        )
        return result(
            DecisionRoute.HUMAN_REVIEW,
            f"Agent confidence {confidence} is below the {confidence_bar} "
            f"autonomy threshold",
            [CONFIDENCE_RULE_ID],
        )

    # ── All gates passed ────────────────────────────────────────────────────
    logger.info(
        "Decision: auto_approve (amount=$%s, category=%s, confidence=%s, ceiling=$%s)",
        enforced_amount, enforced_category, confidence, ceiling,
        extra={"route": "auto_approve", "amount_usd": float(enforced_amount)},
    )
    return result(
        DecisionRoute.AUTO_APPROVE,
        f"Within policy: ${enforced_amount} ≤ ${ceiling} ceiling for "
        f"{enforced_category}, confidence {confidence} ≥ {confidence_bar}, "
        f"no rule violations",
        [],
    )
