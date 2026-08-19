"""Evaluates the configured policy rules against a submission's facts.

A rule's ``when`` is a predicate tree of all / any / not nodes over leaf
comparisons. That covers the whole Northwind policy while staying readable in
JSON, and it executes no caller-supplied code.

Two behaviours worth knowing:

* every matching rule is returned, not just the first. The decision takes the
  most severe outcome and the full list becomes the citation list in the audit
  trail (F9), so document order affects citation order and never the outcome.
* an unknown fact or operator raises. A typo in the policy document must not
  evaluate to "no violation"; the router turns the error into a human escalation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .facts import KNOWN_FACTS, to_decimal
from .policy import PolicyConfig, RuleOutcome

#: Higher wins when several rules match. Duplicate beats reject beats escalate.
OUTCOME_SEVERITY: dict[RuleOutcome, int] = {
    RuleOutcome.HUMAN_REVIEW: 1,
    RuleOutcome.REJECT: 2,
    RuleOutcome.DUPLICATE: 3,
}


class RuleEvaluationError(ValueError):
    """Raised when a rule references an unknown fact or operator."""


@dataclass(frozen=True)
class RuleMatch:
    """One rule that fired."""

    rule_id: str
    outcome: RuleOutcome
    reason: str
    also_cites: tuple[str, ...] = field(default=())

    @property
    def cited_rule_ids(self) -> list[str]:
        return [self.rule_id, *self.also_cites]


# ── Leaf operators ──────────────────────────────────────────────────────────


def _num(value: Any) -> Decimal:
    return to_decimal(value)


def _text(value: Any) -> str:
    return str(value or "").lower()


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _every_item_matches(items: Any, keywords: Any) -> bool:
    """True when *every* line item mentions one of ``keywords``.

    Used by MEAL-03 (alcohol-only receipts). An empty item list is False so the
    rule cannot fire on a submission with no detail at all.
    """
    if not isinstance(items, list) or not items:
        return False
    needles = [_text(k) for k in _as_list(keywords)]
    return all(
        any(needle in _text(item.get("description")) for needle in needles)
        for item in items
        if isinstance(item, dict)
    )


def _any_item_matches(items: Any, keywords: Any) -> bool:
    if not isinstance(items, list):
        return False
    needles = [_text(k) for k in _as_list(keywords)]
    return any(
        any(needle in _text(item.get("description")) for needle in needles)
        for item in items
        if isinstance(item, dict)
    )


def _multiple_of(actual: Any, divisor: Any) -> bool:
    step = _num(divisor)
    if step == 0:
        return False
    value = _num(actual)
    return value != 0 and value % step == 0


OPERATORS: dict[str, Any] = {
    "gt": lambda a, b: _num(a) > _num(b),
    "gte": lambda a, b: _num(a) >= _num(b),
    "lt": lambda a, b: _num(a) < _num(b),
    "lte": lambda a, b: _num(a) <= _num(b),
    "eq": lambda a, b: _num(a) == _num(b) if _is_numeric(b) else _text(a) == _text(b),
    "ne": lambda a, b: not (_num(a) == _num(b) if _is_numeric(b) else _text(a) == _text(b)),
    "in": lambda a, b: _text(a) in [_text(x) for x in _as_list(b)],
    "not_in": lambda a, b: _text(a) not in [_text(x) for x in _as_list(b)],
    "contains_any": lambda a, b: any(_text(x) in _text(a) for x in _as_list(b)),
    "contains_all": lambda a, b: all(_text(x) in _text(a) for x in _as_list(b)),
    "every_item_matches": _every_item_matches,
    "any_item_matches": _any_item_matches,
    "multiple_of": _multiple_of,
    "is_true": lambda a, _b: bool(a) is True,
    "is_false": lambda a, _b: bool(a) is False,
    "missing": lambda a, _b: a is None or a == "" or a == [] or a == 0,
    "present": lambda a, _b: not (a is None or a == "" or a == [] or a == 0),
}

#: Operators that ignore the ``value`` key entirely.
UNARY_OPERATORS = frozenset({"is_true", "is_false", "missing", "present"})


def _is_numeric(value: Any) -> bool:
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return True
    if isinstance(value, str):
        try:
            Decimal(value)
        except Exception:
            return False
        return True
    return False


def _resolve_config_ref(config: PolicyConfig, ref: str) -> Any:
    """Resolve a dotted path such as ``autonomy.confidence_threshold``.

    Lets a rule reuse a configured number instead of repeating it, so there is
    exactly one place to change a threshold.
    """
    target: Any = config
    for part in ref.split("."):
        if isinstance(target, dict):
            if part not in target:
                raise RuleEvaluationError(f"unknown config_ref '{ref}'")
            target = target[part]
        elif hasattr(target, part):
            target = getattr(target, part)
        else:
            raise RuleEvaluationError(f"unknown config_ref '{ref}'")
    return target


# ── Predicate tree ──────────────────────────────────────────────────────────


def evaluate_predicate(
    node: dict[str, Any], facts: dict[str, Any], config: PolicyConfig
) -> bool:
    """Evaluate one predicate node against the facts.

    Raises:
        RuleEvaluationError: on an unknown field, operator or malformed node.
    """
    if not isinstance(node, dict):
        raise RuleEvaluationError(f"predicate must be an object, got {type(node).__name__}")

    if "all" in node:
        return all(evaluate_predicate(child, facts, config) for child in node["all"])
    if "any" in node:
        return any(evaluate_predicate(child, facts, config) for child in node["any"])
    if "not" in node:
        return not evaluate_predicate(node["not"], facts, config)

    field_name = node.get("field")
    op_name = node.get("op")
    if not field_name or not op_name:
        raise RuleEvaluationError(f"predicate needs 'field' and 'op': {node}")
    if field_name not in KNOWN_FACTS:
        raise RuleEvaluationError(f"unknown fact '{field_name}'")
    operator = OPERATORS.get(str(op_name))
    if operator is None:
        raise RuleEvaluationError(f"unknown operator '{op_name}'")

    actual = facts.get(field_name)
    if op_name in UNARY_OPERATORS:
        return bool(operator(actual, None))

    if "config_ref" in node:
        expected = _resolve_config_ref(config, str(node["config_ref"]))
    elif "value" in node:
        expected = node["value"]
    else:
        raise RuleEvaluationError(f"operator '{op_name}' needs 'value' or 'config_ref'")

    # A None fact can never satisfy a comparison (missing attendee count must not
    # accidentally read as "0 per attendee, therefore in policy").
    if actual is None:
        return False
    return bool(operator(actual, expected))


def evaluate_rules(
    config: PolicyConfig,
    facts: dict[str, Any],
    categories: list[str] | None = None,
) -> list[RuleMatch]:
    """Return every enabled rule that matches, in document order.

    Args:
        config: Live policy configuration.
        facts: Normalised facts from :func:`approvalflow.facts.build_facts`.
        categories: Candidate categories whose rules are in scope. Defaults to
            the single category on the facts. The router passes *both* the
            submitted and the agent-assigned category so that re-labelling an
            item cannot shake off a per-category cap.
    """
    scope = categories or [str(facts.get("category", "other"))]
    matches: list[RuleMatch] = []
    for rule in config.enabled_rules_for(scope):
        if evaluate_predicate(rule.when, facts, config):
            matches.append(
                RuleMatch(
                    rule_id=rule.rule_id,
                    outcome=rule.outcome,
                    reason=rule.reason or f"Policy rule {rule.rule_id} matched",
                    also_cites=tuple(rule.also_cites),
                )
            )
    return matches


def most_severe(matches: list[RuleMatch]) -> RuleMatch | None:
    """Pick the deciding match: highest severity, earliest in document order."""
    if not matches:
        return None
    best = matches[0]
    for candidate in matches[1:]:
        if OUTCOME_SEVERITY[candidate.outcome] > OUTCOME_SEVERITY[best.outcome]:
            best = candidate
    return best


# ── Config-time validation ──────────────────────────────────────────────────


def validate_rules_syntax(config: PolicyConfig) -> None:
    """Dry-run every rule against a neutral fact set.

    Called before a new policy document is persisted, so an administrator gets a
    422 with the reason instead of a service that escalates everything at 03:00.

    Raises:
        RuleEvaluationError: if any rule references an unknown fact/operator.
    """
    probe: dict[str, Any] = {name: None for name in KNOWN_FACTS}
    probe.update(
        {
            "amount_usd": Decimal("0"),
            "amount_original": Decimal("0"),
            "currency": "USD",
            "category": "other",
            "line_items": [],
            "line_items_text": "",
            "notes_text": "",
            "line_item_count": 0,
            "max_line_quantity": Decimal("0"),
            "confidence": Decimal("0"),
            "revision": 0,
        }
    )
    for rule in config.rules:
        try:
            evaluate_predicate(rule.when, probe, config)
        except RuleEvaluationError as exc:
            raise RuleEvaluationError(f"rule {rule.rule_id}: {exc}") from exc


def rule_catalogue(config: PolicyConfig) -> list[dict[str, Any]]:
    """Human-readable rule list for the admin UI and the agent prompt."""
    return [
        {
            "rule_id": rule.rule_id,
            "outcome": rule.outcome.value,
            "reason": rule.reason,
            "categories": rule.categories,
            "enabled": rule.enabled,
        }
        for rule in config.rules
    ]


__all__ = [
    "OPERATORS",
    "OUTCOME_SEVERITY",
    "RuleEvaluationError",
    "RuleMatch",
    "evaluate_predicate",
    "evaluate_rules",
    "most_severe",
    "rule_catalogue",
    "validate_rules_syntax",
]
