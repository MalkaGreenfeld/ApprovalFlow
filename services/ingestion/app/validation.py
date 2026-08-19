"""Validation helpers for incoming submissions.

The FX rates come from the policy configuration, so adding a currency or
repricing one is a configuration change (F7) rather than a code change.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from approvalflow.facts import convert_to_usd as _convert_to_usd
from approvalflow.logging import get_logger
from approvalflow.policy import PolicyConfig

logger = get_logger(__name__)

#: Request-body field -> database column, for fields a submitter may amend when
#: answering an information request. ``vendor_known`` is deliberately absent: it
#: is a finance-controlled fact, and letting a submitter assert "this vendor is
#: known" would defeat the GLOBAL-VENDOR hard stop.
AMENDABLE_COLUMNS: dict[str, str] = {
    "notes": "notes",
    "attendees": "attendees",
    "receiptPresent": "receipt_present",
    "lineItems": "line_items",
    "taxAmount": "tax_amount",
    "total": "total",
    "category": "category",
}


def make_idempotency_key(vendor: str, invoice_number: str, total: Decimal | str) -> str:
    """Business identity of an invoice: ``vendor:invoiceNumber:total``.

    Normalised so "Bistro 19" / "bistro 19" and ``42`` / ``42.00`` are the same
    invoice, otherwise a trivial formatting difference defeats duplicate
    detection.
    """
    normalised_total = Decimal(str(total)).quantize(Decimal("0.01"))
    return f"{vendor.strip().lower()}:{invoice_number.strip().lower()}:{normalised_total}"


def convert_to_usd(amount: Decimal, currency: str, config: PolicyConfig) -> Decimal:
    """Convert to USD using the configured FX table."""
    if currency.strip().upper() not in config.fx_rates:
        logger.warning(
            "No FX rate configured for %s; treating it as 1:1 and relying on review",
            currency,
        )
    return _convert_to_usd(amount, currency, config)


def math_reconciles(
    line_items_total: Decimal,
    tax_amount: Decimal,
    declared_total: Decimal,
    *,
    line_item_count: int = 1,
) -> bool:
    """Whether line items plus tax equal the declared total (GLOBAL-MATH).

    With no line items there is no arithmetic to check, so the answer is "no
    disagreement found" rather than False. Returning False conflated two
    different things: "the numbers contradict each other" and "there are no
    numbers", and it made GLOBAL-MATH state that line items do not reconcile on
    a submission that had none. Because the rule applies to every category with
    no lower bound, that single wrong answer meant nothing submitted without
    line-item detail could ever reach auto_approve.

    Missing detail is still caught, by the rule that is actually about it:
    GLOBAL-FRAUD escalates an item with no line items above $100.

    Args:
        line_items_total: Sum of quantity times unit price over the line items.
        tax_amount: Declared tax.
        declared_total: The total the submitter declared.
        line_item_count: How many line items were supplied. Zero means the check
            does not apply.
    """
    if line_item_count <= 0:
        return True
    expected = (line_items_total + tax_amount).quantize(Decimal("0.01"))
    return expected == declared_total.quantize(Decimal("0.01"))


def whitelist_amendments(
    payload: dict[str, Any], config: PolicyConfig
) -> tuple[dict[str, Any], list[str]]:
    """Filter a submitter's amendments down to what policy permits.

    Args:
        payload: Raw ``updates`` object from the info-response request.
        config: Live configuration, whose ``amendable_fields`` list decides what a
            submitter may change.

    Returns:
        ``(column_updates, rejected_field_names)``, rejected fields are reported
        back to the caller rather than dropped in silence.
    """
    allowed = {f for f in config.amendable_fields if f in AMENDABLE_COLUMNS}
    updates: dict[str, Any] = {}
    rejected: list[str] = []

    for field, value in (payload or {}).items():
        if field not in allowed:
            rejected.append(field)
            continue
        column = AMENDABLE_COLUMNS[field]
        if column in ("tax_amount", "total"):
            updates[column] = Decimal(str(value))
        elif column == "attendees":
            updates[column] = int(value) if value not in (None, "") else None
        elif column == "receipt_present":
            updates[column] = bool(value)
        elif column == "line_items":
            updates[column] = [
                {
                    "description": str(item.get("description", "")),
                    "quantity": int(item.get("quantity", 1)),
                    "unitPrice": str(item.get("unitPrice", item.get("unit_price", 0))),
                }
                for item in value or []
                if isinstance(item, dict)
            ]
        else:
            updates[column] = str(value) if value is not None else None

    return updates, rejected
