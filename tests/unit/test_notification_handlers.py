"""Notification, the approver queue surface and the CloudEvent envelope.

The queue itself is SQL now, so its query behaviour is covered by the integration
tests. What is worth asserting without a database is the event-envelope handling
(the source of a whole class of "the handler saw nothing" bugs) and that the
approver API exposes a *structured* send-back rather than a free-text comment.
"""

from __future__ import annotations

from services.notification.app.main import _dec, _extract_event, _pause_key, app

# ── Dapr CloudEvent envelope ────────────────────────────────────────────────


def test_extract_event_unwraps_a_dapr_cloudevent():
    """Dapr wraps the published body in a CloudEvent with a ``data`` field."""
    event = _extract_event(
        {
            "id": "abc",
            "type": "com.dapr.event.sent",
            "topic": "decision.escalated",
            "data": {"correlation_id": "c1", "route": "human_review"},
        }
    )
    assert event == {"correlation_id": "c1", "route": "human_review"}


def test_extract_event_unwraps_a_doubly_wrapped_envelope():
    """Older publishers wrapped the payload themselves; both shapes must work."""
    event = _extract_event(
        {"data": {"data": {"correlation_id": "c1"}, "topic": "x", "pubsubname": "pubsub"}}
    )
    assert event == {"correlation_id": "c1"}


def test_extract_event_passes_a_bare_payload_through():
    event = _extract_event({"correlation_id": "c1"})
    assert event == {"correlation_id": "c1"}


# ── Numeric coercion ────────────────────────────────────────────────────────


def test_amounts_from_events_become_decimal():
    from decimal import Decimal

    assert _dec("42.00") == Decimal("42.00")
    assert _dec(42.0) == Decimal("42.0")
    assert _dec(None) is None
    assert _dec("") is None
    assert _dec("not-a-number") is None


# ── The durable pause token (M11) ───────────────────────────────────────────


def test_the_pause_token_is_keyed_by_correlation_id():
    """Nothing about a paused workflow lives in memory, so a restart is survivable."""
    assert _pause_key("c1") == "hitl:c1"


# ── Service surface ─────────────────────────────────────────────────────────


def test_the_approver_api_is_exposed():
    routes = {r.path for r in app.routes}
    for path in (
        "/health",
        "/api/approvals",
        "/api/approvals/{correlation_id}",
        "/api/approvals/{correlation_id}/approve",
        "/api/approvals/{correlation_id}/reject",
        "/api/approvals/{correlation_id}/send-back",
        "/internal/approvals/{correlation_id}",
    ):
        assert path in routes, f"missing endpoint {path}"


def test_notification_subscribes_to_every_outcome_it_must_report():
    routes = {r.path for r in app.routes}
    for path in (
        "/events/decision-auto-approved",
        "/events/decision-escalated",
        "/events/decision-rejected",
        "/events/decision-duplicate",
        "/events/decision-human-approved",
        "/events/decision-human-rejected",
        "/events/decision-info-requested",
        "/events/submission-info-provided",
        "/events/payment-completed",
        "/events/payment-failed",
        "/events/payment-compensated",
    ):
        assert path in routes, f"missing subscription handler {path}"


def test_send_back_requires_a_question_not_a_bare_comment():
    """F5: "more info needed" is not actionable; the request must say what is needed."""
    import pytest
    from pydantic import ValidationError

    from services.notification.app.main import SendBackAction

    with pytest.raises(ValidationError):
        SendBackAction(approver_email="a@b.c", question="")

    action = SendBackAction(
        approver_email="a@b.c",
        question="Please attach the receipt",
        requested_fields=["receiptPresent"],
    )
    assert action.requested_fields == ["receiptPresent"]


def test_notification_no_longer_writes_submission_status():
    """Ingestion owns submission status; two writers made the outcome a race."""
    import inspect

    from services.notification.app import main as notification_main

    source = inspect.getsource(notification_main)
    assert "ingestion.submissions" not in source
