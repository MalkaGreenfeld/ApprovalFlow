"""The status endpoint explains the outcome in plain language (F2).

Carried over from dev. The mechanism changed: rather than ingestion querying
``router.decisions`` at read time (a cross-schema read into another service's
table), the reason and the cited rules are written onto the submission when the
decision event arrives, and the status endpoint reads its own row. The behaviour
asserted is the same, plus the fallback wording for the statuses that come before
any decision exists.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import services.ingestion.app.main as main
from services.ingestion.app.main import _default_reason

RECORD = {
    "correlation_id": "11111111-1111-4111-8111-111111111111",
    "status": "human_review",
    "decision_reason": "$1820.00 exceeds the $750 autonomy ceiling for meals",
    "rule_ids": ["MEAL-02", "AUTONOMY-CEILING"],
    "vendor": "The Rooftop Grill",
    "amount_usd": "1820.00",
    "currency": "USD",
    "category": "meals",
    "revision": 0,
    "duplicate_of": None,
    "open_info_request": None,
    "info_exchange": [],
    "created_at": "2026-05-16T10:00:00+00:00",
    "updated_at": "2026-05-16T10:00:02+00:00",
}


@pytest.fixture
def client(monkeypatch):
    async def _noop():
        return None

    monkeypatch.setattr(main, "wait_for_pool", _noop)
    monkeypatch.setattr(main, "close_pool", _noop)
    monkeypatch.setattr(main.dispatcher, "start", lambda: None)
    monkeypatch.setattr(main.dispatcher, "stop", _noop)

    async def _pool():
        return object()

    monkeypatch.setattr(main, "get_pool", _pool)
    with TestClient(main.app) as test_client:
        yield test_client


def _serve(monkeypatch, record):
    async def _get_submission(_pool, _cid):
        return record

    monkeypatch.setattr(main.repo, "get_submission", _get_submission)


def test_the_status_carries_the_reason_and_the_rules_that_were_cited(client, monkeypatch):
    _serve(monkeypatch, RECORD)

    body = client.get(f"/api/submissions/{RECORD['correlation_id']}/status").json()

    assert body["status"] == "human_review"
    assert body["reason"] == RECORD["decision_reason"]
    assert body["rule_ids"] == ["MEAL-02", "AUTONOMY-CEILING"]


def test_a_submission_with_no_decision_yet_still_explains_itself(client, monkeypatch):
    _serve(monkeypatch, dict(RECORD, status="received", decision_reason=None, rule_ids=[]))

    body = client.get(f"/api/submissions/{RECORD['correlation_id']}/status").json()

    assert body["reason"] == "Received and queued for analysis."
    assert body["rule_ids"] == []


def test_an_unknown_correlation_id_is_a_404(client, monkeypatch):
    _serve(monkeypatch, None)

    assert client.get("/api/submissions/does-not-exist/status").status_code == 404


@pytest.mark.parametrize(
    ("status_value", "expected"),
    [
        ("received", "Received and queued for analysis."),
        ("reanalyzing", "Your additional information is being re-analysed."),
        ("something_new", "Processing."),
    ],
)
def test_the_fallback_wording_is_defined_for_pre_decision_statuses(status_value, expected):
    assert _default_reason(status_value) == expected
