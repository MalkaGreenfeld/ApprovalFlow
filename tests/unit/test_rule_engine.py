"""The declarative rule engine that replaced the hard-coded rule functions.

The point of these tests is that the *engine* is trustworthy, so a controller can
edit rules as data without a developer re-reading the router each time.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from approvalflow.facts import build_facts
from approvalflow.policy import PolicyConfig, RuleOutcome, parse_policy_config
from approvalflow.ruleengine import (
    RuleEvaluationError,
    RuleMatch,
    evaluate_predicate,
    evaluate_rules,
    most_severe,
    validate_rules_syntax,
)


def config_with(rules: list[dict]) -> PolicyConfig:
    """A minimal valid configuration carrying just the rules under test."""
    return parse_policy_config(
        {
            "version": 1,
            "fx_rates": {"USD": "1.00", "EUR": "1.08"},
            "receipt_required_above_usd": "25",
            "autonomy": {
                "confidence_threshold": "0.85",
                "default_ceiling_usd": "350",
                "category_ceilings_usd": {"meals": "750"},
            },
            "rules": rules,
        }
    )


def facts_for(config: PolicyConfig, **overrides) -> dict:
    submission = {
        "amount_usd": 100,
        "total": 100,
        "category": "meals",
        "currency": "USD",
        "vendor_known": True,
        "receipt_present": True,
        "math_ok": True,
        "attendees": 2,
        "line_items": [{"description": "lunch", "quantity": 1, "unitPrice": "100"}],
        "notes": "",
    }
    submission.update(overrides)
    return build_facts(submission, config=config)


# ── Predicate operators ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("op", "value", "amount", "expected"),
    [
        ("gt", "50", 100, True),
        ("gt", "100", 100, False),
        ("gte", "100", 100, True),
        ("lt", "150", 100, True),
        ("lte", "100", 100, True),
        ("eq", "100", 100, True),
        ("ne", "100", 100, False),
        ("multiple_of", "100", 100, True),
        ("multiple_of", "100", 150, False),
    ],
)
def test_numeric_operators(op, value, amount, expected):
    config = config_with([])
    facts = facts_for(config, amount_usd=amount)
    assert (
        evaluate_predicate({"field": "amount_usd", "op": op, "value": value}, facts, config)
        is expected
    )


def test_decimal_comparison_is_exact_not_floating_point():
    """0.1 + 0.2 problems must not decide whether an invoice is over a ceiling."""
    config = config_with([])
    facts = facts_for(config, amount_usd="750.00")
    assert not evaluate_predicate(
        {"field": "amount_usd", "op": "gt", "value": "750"}, facts, config
    )
    assert isinstance(facts["amount_usd"], Decimal)


def test_contains_any_is_case_insensitive():
    config = config_with([])
    facts = facts_for(config, notes="Flew BUSINESS Class to Berlin")
    assert evaluate_predicate(
        {"field": "notes_text", "op": "contains_any", "value": ["business class"]},
        facts,
        config,
    )


def test_every_item_matches_needs_all_line_items():
    """MEAL-03 rejects alcohol-*only* receipts, not any receipt mentioning wine."""
    config = config_with([])
    all_alcohol = facts_for(
        config,
        line_items=[
            {"description": "Red wine (alcohol)", "quantity": 1, "unitPrice": "30"},
            {"description": "Whisky (alcohol)", "quantity": 1, "unitPrice": "40"},
        ],
    )
    mixed = facts_for(
        config,
        line_items=[
            {"description": "Red wine (alcohol)", "quantity": 1, "unitPrice": "30"},
            {"description": "Steak", "quantity": 1, "unitPrice": "40"},
        ],
    )
    predicate = {"field": "line_items", "op": "every_item_matches", "value": ["alcohol"]}

    assert evaluate_predicate(predicate, all_alcohol, config)
    assert not evaluate_predicate(predicate, mixed, config)


def test_every_item_matches_is_false_for_an_empty_receipt():
    """No detail at all must not read as "everything on it was alcohol"."""
    config = config_with([])
    facts = facts_for(config, line_items=[])
    assert not evaluate_predicate(
        {"field": "line_items", "op": "every_item_matches", "value": ["alcohol"]},
        facts,
        config,
    )


def test_missing_attendee_count_is_missing_not_zero():
    """A missing count must not divide to a comfortable $0 per attendee."""
    config = config_with([])
    facts = facts_for(config, attendees=None)
    assert facts["attendees"] is None
    assert facts["amount_per_attendee"] is None
    assert evaluate_predicate({"field": "attendees", "op": "missing"}, facts, config)
    # A comparison against a missing fact can never be satisfied.
    assert not evaluate_predicate(
        {"field": "amount_per_attendee", "op": "gt", "value": "75"}, facts, config
    )


def test_config_ref_reads_the_threshold_from_configuration():
    """A rule can reference a configured number instead of repeating it."""
    config = config_with([])
    facts = facts_for(config, amount_usd=30)
    assert evaluate_predicate(
        {"field": "amount_usd", "op": "gt", "config_ref": "receipt_required_above_usd"},
        facts,
        config,
    )
    assert evaluate_predicate(
        {
            "field": "amount_usd",
            "op": "gt",
            "config_ref": "autonomy.default_ceiling_usd",
        },
        facts,
        config,
    ) is False


def test_boolean_and_nested_predicates():
    config = config_with([])
    facts = facts_for(config, receipt_present=False, amount_usd=100)
    predicate = {
        "all": [
            {"field": "receipt_present", "op": "is_false"},
            {
                "any": [
                    {"field": "amount_usd", "op": "gt", "value": "25"},
                    {"field": "vendor_known", "op": "is_false"},
                ]
            },
            {"not": {"field": "math_ok", "op": "is_false"}},
        ]
    }
    assert evaluate_predicate(predicate, facts, config)


# ── Failing loudly ──────────────────────────────────────────────────────────


def test_unknown_fact_raises_instead_of_never_matching():
    """A typo'd field name must be an error, not a rule that silently never fires."""
    config = config_with([])
    with pytest.raises(RuleEvaluationError, match="unknown fact"):
        evaluate_predicate(
            {"field": "amont_usd", "op": "gt", "value": "10"}, facts_for(config), config
        )


def test_unknown_operator_raises():
    config = config_with([])
    with pytest.raises(RuleEvaluationError, match="unknown operator"):
        evaluate_predicate(
            {"field": "amount_usd", "op": "exceeds", "value": "10"},
            facts_for(config),
            config,
        )


def test_validate_rules_syntax_catches_a_bad_rule_before_it_is_saved():
    """A bad rule must fail validation, so the admin API returns 422."""
    config = config_with(
        [
            {
                "rule_id": "TYPO-01",
                "outcome": "human_review",
                "when": {"field": "nonexistent", "op": "gt", "value": "1"},
            }
        ]
    )
    with pytest.raises(RuleEvaluationError, match="TYPO-01"):
        validate_rules_syntax(config)


def test_validate_rules_syntax_accepts_the_shipped_catalogue(policy_config):
    """The configuration we ship must itself pass the gate we impose on updates."""
    validate_rules_syntax(policy_config)


# ── Matching and severity ───────────────────────────────────────────────────


def test_only_rules_for_the_category_are_evaluated():
    config = config_with(
        [
            {
                "rule_id": "SAAS-01",
                "outcome": "human_review",
                "categories": ["saas"],
                "when": {"field": "amount_usd", "op": "gt", "value": "1"},
            }
        ]
    )
    assert evaluate_rules(config, facts_for(config, category="meals")) == []
    assert len(evaluate_rules(config, facts_for(config, category="saas"))) == 1


def test_disabled_rules_are_skipped():
    config = config_with(
        [
            {
                "rule_id": "OFF-01",
                "outcome": "human_review",
                "enabled": False,
                "when": {"field": "amount_usd", "op": "gt", "value": "1"},
            }
        ]
    )
    assert evaluate_rules(config, facts_for(config)) == []


def test_every_matching_rule_is_returned_for_the_audit_trail():
    config = config_with(
        [
            {
                "rule_id": "A-01",
                "outcome": "human_review",
                "when": {"field": "amount_usd", "op": "gt", "value": "1"},
            },
            {
                "rule_id": "B-01",
                "outcome": "human_review",
                "when": {"field": "receipt_present", "op": "is_true"},
            },
        ]
    )
    matches = evaluate_rules(config, facts_for(config))
    assert [m.rule_id for m in matches] == ["A-01", "B-01"]


def test_reject_outranks_escalation_regardless_of_document_order():
    """Reordering the configuration must not change an outcome."""
    escalate = RuleMatch("A-01", RuleOutcome.HUMAN_REVIEW, "escalate")
    reject = RuleMatch("B-01", RuleOutcome.REJECT, "reject")

    assert most_severe([escalate, reject]).rule_id == "B-01"
    assert most_severe([reject, escalate]).rule_id == "B-01"


def test_duplicate_outranks_everything():
    """A repeat submission must be short-circuited, not re-judged on its merits."""
    reject = RuleMatch("B-01", RuleOutcome.REJECT, "reject")
    duplicate = RuleMatch("GLOBAL-DUP", RuleOutcome.DUPLICATE, "duplicate")
    assert most_severe([reject, duplicate]).rule_id == "GLOBAL-DUP"


def test_also_cites_appear_in_the_citation_list():
    """HW-02 cites HW-01 alongside it, so the trail names both clauses."""
    match = RuleMatch("HW-02", RuleOutcome.HUMAN_REVIEW, "capital", ("HW-01",))
    assert match.cited_rule_ids == ["HW-02", "HW-01"]


# ── Subscription caps are per month, not per invoice ────────────────────────


def test_an_annual_subscription_is_normalised_to_a_monthly_figure(policy_config):
    """SAAS-01 caps spend *per month*.

    A $300 annual plan is $25/month and is comfortably in policy. Comparing the
    invoice total against the monthly cap escalated every annual renewal.
    """
    facts = build_facts(
        {
            "category": "saas",
            "vendor_known": True,
            "receipt_present": True,
            "math_ok": True,
            "currency": "USD",
            "total": 300,
            "amount_usd": 300,
            "line_items": [
                {"description": "Design tool - annual plan", "quantity": 1, "unitPrice": 300}
            ],
            "notes": "",
        },
        config=policy_config,
    )

    assert facts["billing_period"] == "annual"
    assert facts["amount_usd_monthly"] == Decimal("25.00")
    assert [m.rule_id for m in evaluate_rules(policy_config, facts)] == []


def test_a_monthly_subscription_over_the_cap_still_escalates(policy_config):
    facts = build_facts(
        {
            "category": "saas",
            "vendor_known": True,
            "receipt_present": True,
            "math_ok": True,
            "currency": "USD",
            "total": 220,
            "amount_usd": 220,
            "line_items": [
                {"description": "Monitoring - monthly subscription", "quantity": 1, "unitPrice": 220}
            ],
            "notes": "",
        },
        config=policy_config,
    )

    assert facts["billing_period"] == "monthly"
    assert facts["amount_usd_monthly"] == Decimal("220.00")
    assert "SAAS-01" in [m.rule_id for m in evaluate_rules(policy_config, facts)]


def test_an_unstated_billing_period_is_assumed_to_be_monthly(policy_config):
    """The strict assumption: guessing "annual" would understate monthly cost."""
    facts = build_facts(
        {
            "category": "saas",
            "vendor_known": True,
            "receipt_present": True,
            "math_ok": True,
            "currency": "USD",
            "total": 500,
            "amount_usd": 500,
            "line_items": [{"description": "Some tool", "quantity": 1, "unitPrice": 500}],
            "notes": "",
        },
        config=policy_config,
    )

    assert facts["billing_period"] == "monthly"
    assert "SAAS-01" in [m.rule_id for m in evaluate_rules(policy_config, facts)]
