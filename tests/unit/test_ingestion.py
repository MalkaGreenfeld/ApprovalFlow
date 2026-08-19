"""Ingestion, intake validation, idempotency and the amendment whitelist.

These replace the previous source-introspection tests, which asserted that
particular strings appeared in the function body. Those passed whether or not the
behaviour was correct and broke on any refactor, so they are gone; what is checked
here is what the code does.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from approvalflow.facts import convert_to_usd
from approvalflow.models import LineItem, SubmissionRequest
from services.ingestion.app.validation import (
    AMENDABLE_COLUMNS,
    make_idempotency_key,
    math_reconciles,
    whitelist_amendments,
)

# ── Money is Decimal all the way through ────────────────────────────────────


def test_amounts_stay_decimal_through_parsing():
    """A float anywhere in the money path is a rounding bug waiting to happen."""
    request = SubmissionRequest(
        submitter="test@test.com",
        department="engineering-2026Q2",
        vendor="TestVendor",
        vendorKnown=True,
        invoiceNumber="INV-001",
        currency="USD",
        category="meals",
        attendees=1,
        lineItems=[LineItem(description="lunch", quantity=1, unitPrice=Decimal("38.89"))],
        taxAmount=Decimal("3.11"),
        total=Decimal("42.00"),
    )

    assert isinstance(request.total, Decimal)
    assert isinstance(request.taxAmount, Decimal)
    assert isinstance(request.lineItems[0].unit_price, Decimal)


def test_math_reconciliation_is_exact(policy_config):
    assert math_reconciles(Decimal("38.89"), Decimal("3.11"), Decimal("42.00"))
    assert not math_reconciles(Decimal("300.00"), Decimal("0"), Decimal("3000.00"))
    # A cent of drift is a mismatch, not a rounding tolerance.
    assert not math_reconciles(Decimal("38.89"), Decimal("3.11"), Decimal("42.01"))


def test_fx_conversion_comes_from_configuration(policy_config):
    """F7: adding or repricing a currency is a configuration change."""
    assert convert_to_usd(Decimal("1200"), "EUR", policy_config) == Decimal("1296.00")
    assert convert_to_usd(Decimal("100"), "GBP", policy_config) == Decimal("127.00")
    assert convert_to_usd(Decimal("100"), "USD", policy_config) == Decimal("100.00")


def test_an_unknown_currency_is_treated_one_to_one_not_dropped(policy_config):
    """It must still produce a number a human can review, never a crash or a zero."""
    assert convert_to_usd(Decimal("500"), "XYZ", policy_config) == Decimal("500.00")


# ── Business idempotency key (F3 / M10) ─────────────────────────────────────


def test_the_business_key_is_vendor_invoice_and_total():
    key = make_idempotency_key("Bistro 19", "NW-INV-7781", Decimal("42.00"))
    assert key == "bistro 19:nw-inv-7781:42.00"


def test_formatting_differences_do_not_defeat_duplicate_detection():
    """"42" and "42.00", "Bistro 19" and "BISTRO 19" are the same invoice."""
    a = make_idempotency_key("Bistro 19", "NW-INV-7781", Decimal("42"))
    b = make_idempotency_key("  BISTRO 19  ", "nw-inv-7781", Decimal("42.00"))
    assert a == b


def test_different_invoices_get_different_keys():
    a = make_idempotency_key("Bistro 19", "NW-INV-7781", Decimal("42.00"))
    b = make_idempotency_key("Bistro 19", "NW-INV-7782", Decimal("42.00"))
    c = make_idempotency_key("Bistro 19", "NW-INV-7781", Decimal("43.00"))
    assert len({a, b, c}) == 3


# ── The amendment whitelist (F5 security boundary) ──────────────────────────


def test_a_submitter_can_supply_what_was_asked_for(policy_config):
    updates, rejected = whitelist_amendments(
        {"receiptPresent": True, "attendees": 4, "notes": "Client dinner for Acme"},
        policy_config,
    )

    assert updates == {
        "receipt_present": True,
        "attendees": 4,
        "notes": "Client dinner for Acme",
    }
    assert rejected == []


def test_a_submitter_cannot_declare_their_vendor_known(policy_config):
    """The GLOBAL-VENDOR hard stop must not be switchable off by its subject."""
    updates, rejected = whitelist_amendments(
        {"vendorKnown": True, "vendor": "Totally Legit Ltd"}, policy_config
    )

    assert updates == {}
    assert set(rejected) == {"vendorKnown", "vendor"}
    assert "vendorKnown" not in AMENDABLE_COLUMNS.values()


def test_a_submitter_cannot_amend_arbitrary_columns(policy_config):
    """Nothing outside the whitelist reaches the UPDATE statement."""
    updates, rejected = whitelist_amendments(
        {"status": "paid", "correlation_id": "other", "amount_usd": "1"}, policy_config
    )

    assert updates == {}
    assert set(rejected) == {"status", "correlation_id", "amount_usd"}


def test_amended_amounts_are_coerced_to_decimal(policy_config):
    """An amended total must enter the ceiling comparison as an exact number."""
    updates, _ = whitelist_amendments({"total": "1234.56"}, policy_config)
    assert updates["total"] == Decimal("1234.56")


def test_amended_line_items_are_normalised(policy_config):
    updates, _ = whitelist_amendments(
        {"lineItems": [{"description": "Taxi", "quantity": 2, "unit_price": 30}]},
        policy_config,
    )
    assert updates["line_items"] == [
        {"description": "Taxi", "quantity": 2, "unitPrice": "30"}
    ]


def test_malformed_line_items_are_ignored_rather_than_crashing(policy_config):
    updates, _ = whitelist_amendments({"lineItems": ["nonsense", None]}, policy_config)
    assert updates["line_items"] == []


# ── The service surface ─────────────────────────────────────────────────────


def test_ingestion_exposes_the_submitter_and_auditor_endpoints():
    from services.ingestion.app.main import app

    routes = {r.path for r in app.routes}
    for path in (
        "/health",
        "/api/submissions",
        "/api/submissions/{correlation_id}/status",
        "/api/submissions/{correlation_id}/audit",
        "/api/submissions/{correlation_id}/info-response",
        "/api/auth/token",
    ):
        assert path in routes, f"missing endpoint {path}"


def test_every_decision_and_payment_event_has_a_status_and_a_handler():
    """The event-to-status map and the subscriptions must not drift apart."""
    from services.ingestion.app.main import STATUS_BY_EVENT, app

    routes = {r.path for r in app.routes}
    for topic in STATUS_BY_EVENT:
        path = "/events/" + topic.replace(".", "-").replace("_", "-")
        assert path in routes, f"{topic} is mapped to a status but has no handler"


def test_terminal_statuses_cannot_be_overwritten():
    """A redelivered event must not resurrect a paid or rejected submission."""
    from services.ingestion.app.repository import TERMINAL_STATUSES

    for status in ("paid", "rejected", "human_rejected", "duplicate", "compensated"):
        assert status in TERMINAL_STATUSES


@pytest.mark.parametrize(
    "topic",
    ["decision.auto_approved", "decision.escalated", "payment.completed"],
)
def test_status_mapping_is_explicit(topic):
    from services.ingestion.app.main import STATUS_BY_EVENT

    assert STATUS_BY_EVENT[topic]
