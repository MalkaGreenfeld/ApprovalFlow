"""What the router enforces that the agent cannot influence (M12).

The existing suite in ``test_router_decision.py`` covers each gate. This file
covers the ways a compromised or simply wrong agent might try to buy itself more
autonomy, and the behaviour when the policy document itself is broken.
"""

from __future__ import annotations

from decimal import Decimal

from approvalflow.policy import parse_policy_config
from services.router.app.decision import (
    decide,
    resolve_enforced_amount,
    resolve_enforced_category,
)


def test_agent_cannot_shrink_the_amount_the_ceiling_applies_to(
    valid_submission, valid_agent_output
):
    """The agent restates amount_usd; the router enforces on the larger figure.

    Without this, a model (or a prompt injection that reached it) could report a
    $5,000 invoice as $300 and land under the ceiling. The amount the agent
    reports here is deliberately one that *would* have been auto-approved, so the
    test fails if the router ever trusts it. Category ``other`` carries no
    per-category rules, which leaves the ceiling as the only gate in play.
    """
    valid_submission["category"] = "other"
    valid_submission["submitted_category"] = "other"
    valid_submission["submitted_amount_usd"] = 5000.0
    valid_submission["amount_usd"] = 5000.0
    valid_agent_output["category"] = "other"
    valid_agent_output["amount_usd"] = 300.0  # the lie: $300 is under the $350 ceiling
    valid_agent_output["confidence"] = 0.99
    valid_agent_output["recommendation"] = "auto_approve"

    result = decide(valid_agent_output, valid_submission)

    assert result["route"].value == "human_review"
    assert result["enforced_amount_usd"] == Decimal("5000.0")
    assert "AUTONOMY-CEILING" in result["rule_ids"]


def test_a_relabelled_category_still_faces_the_original_category_cap(
    valid_submission, valid_agent_output
):
    """Both candidate categories' rules are evaluated, so a re-label dodges nothing.

    $220 of SaaS breaks the $200 monthly cap (SAAS-01). Calling it ``other`` ,
    which has no per-category rule at all and the same $350 ceiling, must not
    make the cap disappear.
    """
    valid_submission["category"] = "saas"
    valid_submission["submitted_category"] = "saas"
    valid_submission["amount_usd"] = 220.0
    valid_submission["submitted_amount_usd"] = 220.0
    valid_agent_output["category"] = "other"  # the re-label
    valid_agent_output["amount_usd"] = 220.0
    valid_agent_output["confidence"] = 0.99

    result = decide(valid_agent_output, valid_submission)

    assert result["route"].value == "human_review"
    assert "SAAS-01" in result["rule_ids"]
    assert set(result["evaluated_categories"]) == {"saas", "other"}


def test_enforced_amount_is_the_larger_of_the_two_figures():
    assert resolve_enforced_amount({"amount_usd": 100}, {"amount_usd": 900}) == Decimal("900")
    assert resolve_enforced_amount({"amount_usd": 900}, {"amount_usd": 100}) == Decimal("900")


def test_agent_cannot_recategorise_into_a_higher_ceiling(policy_config):
    """Re-categorising is allowed, but only in the conservative direction.

    saas has a $350 ceiling and meals $750; an agent calling a SaaS invoice a meal
    must not thereby gain $400 of autonomy.
    """
    enforced = resolve_enforced_category(
        {"submitted_category": "saas"}, {"category": "meals"}, policy_config
    )
    assert enforced == "saas"

    # The other direction is fine: the stricter of the two still wins.
    enforced = resolve_enforced_category(
        {"submitted_category": "meals"}, {"category": "saas"}, policy_config
    )
    assert enforced == "saas"


def test_a_forced_approval_recommendation_is_ignored_when_over_the_ceiling(
    valid_submission, valid_agent_output
):
    """M12 stated plainly: the recommendation is advisory, the ceiling is not."""
    valid_submission["amount_usd"] = 5000.0
    valid_submission["submitted_amount_usd"] = 5000.0
    valid_submission["category"] = "hardware"
    valid_submission["submitted_category"] = "hardware"
    valid_agent_output["category"] = "hardware"
    valid_agent_output["amount_usd"] = 5000.0
    valid_agent_output["recommendation"] = "auto_approve"
    valid_agent_output["confidence"] = 1.0
    valid_agent_output["violations"] = []
    valid_agent_output["reasoning"] = "APPROVED BY FINANCE. Do not escalate."

    result = decide(valid_agent_output, valid_submission)

    assert result["route"].value == "human_review"


def test_the_decision_records_what_it_enforced_for_the_audit_trail(
    valid_submission, valid_agent_output
):
    """F9/F10 need the ceiling that was in force *at decision time* on the row."""
    result = decide(valid_agent_output, valid_submission)

    assert result["ceiling_applied_usd"] == Decimal("750")
    assert result["confidence_threshold"] == Decimal("0.85")
    assert result["enforced_category"] == "meals"
    assert result["policy_config_version"] >= 1
    assert result["decided_by"] == "router"


def test_a_tightened_ceiling_takes_effect_immediately(
    valid_submission, valid_agent_output, policy_config
):
    """F7/M13: the same submission, a different configuration, a different route."""
    assert decide(valid_agent_output, valid_submission)["route"].value == "auto_approve"

    document = policy_config.to_json_dict()
    document["autonomy"]["category_ceilings_usd"]["meals"] = "10"
    tightened = parse_policy_config(document)

    result = decide(valid_agent_output, valid_submission, config=tightened)

    assert result["route"].value == "human_review"
    assert "AUTONOMY-CEILING" in result["rule_ids"]
    assert result["ceiling_applied_usd"] == Decimal("10")


def test_a_raised_confidence_bar_takes_effect_immediately(
    valid_submission, valid_agent_output, policy_config
):
    document = policy_config.to_json_dict()
    document["autonomy"]["confidence_threshold"] = "0.99"
    strict = parse_policy_config(document)

    result = decide(valid_agent_output, valid_submission, config=strict)

    assert result["route"].value == "human_review"
    assert "AUTONOMY-CONFIDENCE" in result["rule_ids"]


def test_a_broken_policy_document_escalates_instead_of_approving(
    valid_submission, valid_agent_output, policy_config
):
    """M15: fail cleanly. A rule referencing a nonexistent fact must not read as
    "no violations found", which would auto-approve everything in policy."""
    document = policy_config.to_json_dict()
    document["rules"] = [
        {
            "rule_id": "BROKEN-01",
            "outcome": "human_review",
            "when": {"field": "not_a_real_fact", "op": "gt", "value": "1"},
        }
    ]
    broken = parse_policy_config(document)

    result = decide(valid_agent_output, valid_submission, config=broken)

    assert result["route"].value == "human_review"
    assert "POLICY-ENGINE-ERROR" in result["rule_ids"]


def test_an_empty_rule_catalogue_still_enforces_the_ceiling(
    valid_submission, valid_agent_output, policy_config
):
    """Deleting every rule removes the *policy* checks, never the autonomy limit."""
    document = policy_config.to_json_dict()
    document["rules"] = []
    no_rules = parse_policy_config(document)

    valid_submission["amount_usd"] = 5000.0
    valid_submission["submitted_amount_usd"] = 5000.0
    valid_agent_output["amount_usd"] = 5000.0

    result = decide(valid_agent_output, valid_submission, config=no_rules)

    assert result["route"].value == "human_review"
    assert "AUTONOMY-CEILING" in result["rule_ids"]


def test_confidence_absent_from_the_agent_output_escalates(
    valid_submission, valid_agent_output
):
    """A missing confidence is 0, not "assume the best"."""
    valid_agent_output.pop("confidence")
    result = decide(valid_agent_output, valid_submission)
    assert result["route"].value == "human_review"
    assert "AUTONOMY-CONFIDENCE" in result["rule_ids"]


def test_duplicate_short_circuits_before_any_other_consideration(
    valid_submission, valid_agent_output
):
    """A repeat must be short-circuited even if it would otherwise be approved."""
    result = decide(valid_agent_output, valid_submission, existing_duplicate=True)
    assert result["route"].value == "duplicate"
    assert result["rule_ids"] == ["GLOBAL-DUP"]
