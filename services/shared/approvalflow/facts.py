"""Normalised facts about a submission: the only input the rule engine sees.

The agent (for retrieval) and the router (for the decision) both build facts
here, so what the model looked at and what the router enforced come from one
typed vocabulary instead of ad-hoc dict.get calls in each service.

Money becomes Decimal and text becomes lower-case, so rules match
case-insensitively without every rule having to remember to.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .policy import PolicyConfig

#: Fact names the rule engine may reference. Anything else is a config error,
#: which keeps typos in the policy document from silently never matching.
KNOWN_FACTS: frozenset[str] = frozenset(
    {
        "amount_usd",
        "amount_original",
        "currency",
        "is_foreign_currency",
        "category",
        "submitted_category",
        "vendor",
        "vendor_known",
        "receipt_present",
        "math_ok",
        "attendees",
        "amount_per_attendee",
        "billing_period",
        "amount_usd_monthly",
        "line_items",
        "line_item_count",
        "has_line_items",
        "line_items_text",
        "has_undescribed_line_item",
        "max_line_quantity",
        "notes_text",
        "is_duplicate",
        "confidence",
        "department",
        "revision",
    }
)


def to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    """Coerce anything JSON or SQL can hand us into a ``Decimal``.

    Events carry floats, PostgreSQL hands back ``Decimal``, the state store
    hands back strings. All three must compare identically against a ceiling.
    """
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


#: How many months a billing period covers, for normalising subscription caps.
BILLING_PERIOD_MONTHS: dict[str, int] = {"monthly": 1, "quarterly": 3, "annual": 12}

#: Wording that identifies a billing period in a description or note. Longest
#: match wins, and "monthly" is the assumption when nothing says otherwise ,
#: assuming a longer period would understate the monthly cost and be less strict.
_BILLING_PERIOD_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("annual", ("annual", "yearly", "per year", "/yr", "/year", "12 month", "1 year")),
    ("quarterly", ("quarterly", "per quarter", "/qtr", "3 month")),
    ("monthly", ("monthly", "per month", "/mo", "1 month")),
)


def detect_billing_period(text: str) -> str:
    """Infer the billing period a subscription invoice covers.

    ``SAAS-01`` caps subscriptions *per month*, so applying it to the invoice
    total treats a $300 annual plan ($25/month) as though it broke a $200
    monthly cap. This lets the rule compare like with like.
    """
    haystack = text.lower()
    for period, needles in _BILLING_PERIOD_HINTS:
        if any(needle in haystack for needle in needles):
            return period
    return "monthly"


def _normalise_line_items(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        description = entry.get("description") or ""
        unit_price = entry.get("unitPrice", entry.get("unit_price", 0))
        items.append(
            {
                "description": str(description).strip().lower(),
                "quantity": to_decimal(entry.get("quantity", 1), Decimal("1")),
                "unit_price": to_decimal(unit_price),
            }
        )
    return items


def build_facts(
    submission: dict[str, Any],
    *,
    config: PolicyConfig,
    is_duplicate: bool = False,
    amount_usd: Decimal | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """Build the fact dictionary for one submission.

    Args:
        submission: Event payload or database row for the submission.
        config: Live policy configuration (used for FX conversion).
        is_duplicate: Authoritative duplicate flag decided at intake.
        amount_usd: Override for the USD amount. The router passes the
            *conservative* amount here so the agent cannot shrink the number the
            ceiling is applied to.
        category: Override for the category, same reasoning.

    Returns:
        A dict whose keys are all in :data:`KNOWN_FACTS`.
    """
    original = to_decimal(submission.get("total", submission.get("amount_original", 0)))
    currency = str(submission.get("currency", config.base_currency) or "USD").upper()

    resolved_amount = (
        amount_usd
        if amount_usd is not None
        else to_decimal(submission.get("amount_usd"), convert_to_usd(original, currency, config))
    )
    resolved_category = str(
        category if category is not None else submission.get("category", "other") or "other"
    ).strip().lower()

    line_items = _normalise_line_items(submission.get("line_items", submission.get("lineItems")))
    attendees_raw = submission.get("attendees")
    attendees = int(attendees_raw) if isinstance(attendees_raw, (int, float)) and attendees_raw else None

    notes_text = str(submission.get("notes") or "").strip().lower()
    line_items_text = " ".join(item["description"] for item in line_items)
    billing_period = detect_billing_period(f"{line_items_text} {notes_text}")
    months = BILLING_PERIOD_MONTHS[billing_period]

    return {
        "amount_usd": resolved_amount,
        "amount_original": original,
        "currency": currency,
        "is_foreign_currency": currency != config.base_currency.upper(),
        "category": resolved_category,
        "submitted_category": str(submission.get("category", "") or "").strip().lower(),
        "vendor": str(submission.get("vendor", "") or ""),
        "vendor_known": bool(submission.get("vendor_known", False)),
        "receipt_present": bool(submission.get("receipt_present", False)),
        "math_ok": bool(submission.get("math_ok", True)),
        "attendees": attendees,
        "amount_per_attendee": (resolved_amount / attendees) if attendees else None,
        "billing_period": billing_period,
        "amount_usd_monthly": (resolved_amount / months).quantize(Decimal("0.01")),
        "line_items": line_items,
        "line_item_count": len(line_items),
        "has_line_items": bool(line_items),
        "line_items_text": line_items_text,
        "has_undescribed_line_item": any(not item["description"] for item in line_items),
        "max_line_quantity": max(
            (item["quantity"] for item in line_items), default=Decimal("0")
        ),
        "notes_text": notes_text,
        "is_duplicate": bool(is_duplicate),
        "confidence": to_decimal(submission.get("confidence", 0)),
        "department": str(submission.get("department", "") or ""),
        "revision": int(submission.get("revision", 0) or 0),
    }


def convert_to_usd(amount: Decimal, currency: str, config: PolicyConfig) -> Decimal:
    """Convert to USD using the *configured* FX table (F7, no code change).

    An unknown currency is treated as 1:1 and is a data-quality problem the
    ``GLOBAL-FX`` rule and the human reviewer will see, never a silent success.
    """
    from decimal import ROUND_HALF_UP

    rate = config.fx_rates.get(currency.strip().upper(), Decimal("1"))
    return (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
