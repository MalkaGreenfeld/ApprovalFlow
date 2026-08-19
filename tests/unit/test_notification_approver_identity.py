"""An approval must be attributable to somebody (F9).

Carried over from dev. With authentication enabled the verified token supplies
the identity; these tests run with AUTH_ENABLED=false (see tests/conftest.py),
where the request body is the only source, so a blank value has to be refused
rather than recorded as "unknown".
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import services.notification.app.main as main
from services.notification.app.main import _require_approver_email


@pytest.fixture
def client(monkeypatch):
    """A client whose startup does not reach for PostgreSQL.

    The validation under test runs before any database work, so the pool and the
    outbox dispatcher are stubbed out to keep this a unit test.
    """

    async def _noop():
        return None

    monkeypatch.setattr(main, "wait_for_pool", _noop)
    monkeypatch.setattr(main, "close_pool", _noop)
    monkeypatch.setattr(main.dispatcher, "start", lambda: None)
    monkeypatch.setattr(main.dispatcher, "stop", _noop)
    with TestClient(main.app) as test_client:
        yield test_client


BODIES = {
    "approve": {"comment": "ok"},
    "reject": {"comment": "no"},
    "send-back": {"question": "Which client was this for?"},
}


@pytest.mark.parametrize("endpoint", ["approve", "reject", "send-back"])
def test_a_missing_approver_email_is_refused(client, endpoint):
    resp = client.post(f"/api/approvals/corr-1/{endpoint}", json=BODIES[endpoint])

    assert resp.status_code == 400
    assert "approver_email" in resp.json()["detail"]


@pytest.mark.parametrize("endpoint", ["approve", "reject", "send-back"])
def test_a_blank_approver_email_is_refused(client, endpoint):
    body = dict(BODIES[endpoint], approver_email="   ")
    resp = client.post(f"/api/approvals/corr-1/{endpoint}", json=body)

    assert resp.status_code == 400
    assert "approver_email" in resp.json()["detail"]


def test_the_email_is_trimmed():
    assert _require_approver_email({"approver_email": "  a@test.com  "}) == "a@test.com"


def test_absent_and_blank_both_read_as_no_identity():
    assert _require_approver_email({}) is None
    assert _require_approver_email({"approver_email": ""}) is None
    assert _require_approver_email({"approver_email": "   "}) is None
