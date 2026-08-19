"""A full journey leaves a complete audit trail (F9).

Carried over from dev. It used to run the ARCHITECTURE.md audit JOIN directly
against PostgreSQL; it now asks the audit endpoint instead, so the test asserts
the contract an auditor actually uses and does not have to be edited whenever a
column is renamed.

Needs the compose stack running (`docker compose up -d`); skipped otherwise.
"""

from __future__ import annotations

import asyncio
import socket
import time

import httpx
import pytest

GATEWAY = "http://localhost:8080"
API = f"{GATEWAY}/api"

pytestmark = pytest.mark.asyncio


def _gateway_reachable() -> bool:
    try:
        with socket.create_connection(("localhost", 8080), timeout=2):
            return True
    except OSError:
        return False


IN_POLICY = {
    "submitter": "dana.cohen@northwind.example",
    "department": "engineering-2026Q2",
    "vendor": "Bistro 19",
    "vendorKnown": True,
    "currency": "USD",
    "category": "meals",
    "attendees": 1,
    "lineItems": [{"description": "Team lunch", "quantity": 1, "unitPrice": 38.89}],
    "taxAmount": 3.11,
    "total": 42.0,
    "receiptPresent": True,
    "notes": "Solo working lunch.",
}


async def _token(client: httpx.AsyncClient, subject: str, roles: list[str]) -> str:
    resp = await client.post(
        f"{API}/auth/token", json={"subject": subject, "roles": roles}
    )
    resp.raise_for_status()
    return str(resp.json()["access_token"])


async def test_an_auto_approved_journey_produces_a_complete_audit_trail():
    if not _gateway_reachable():
        pytest.skip("compose stack not running on localhost:8080")

    async with httpx.AsyncClient(timeout=30) as client:
        submitter = await _token(client, "dana.cohen@northwind.example", ["submitter"])
        auditor = await _token(client, "auditor@northwind.example", ["admin"])

        payload = dict(IN_POLICY, invoiceNumber=f"AUDIT-{int(time.time())}")
        created = await client.post(
            f"{API}/submissions",
            json=payload,
            headers={"Authorization": f"Bearer {submitter}"},
        )
        assert created.status_code in (200, 202), created.text
        correlation_id = created.json()["correlation_id"]

        # Wait for the item to reach a terminal state.
        deadline = time.time() + 90
        status = ""
        while time.time() < deadline:
            resp = await client.get(
                f"{API}/submissions/{correlation_id}/status",
                headers={"Authorization": f"Bearer {submitter}"},
            )
            status = resp.json().get("status", "")
            if status in ("paid", "rejected", "duplicate", "compensated"):
                break
            await asyncio.sleep(1)

        assert status == "paid", f"expected paid, got {status!r}"

        trail = await client.get(
            f"{API}/submissions/{correlation_id}/audit",
            headers={"Authorization": f"Bearer {auditor}"},
        )
        assert trail.status_code == 200, trail.text
        body = trail.json()

    # Extracted data, the decision, the timeline and the payment outcome, all
    # reachable from the one correlation id.
    assert body["correlation_id"] == correlation_id
    assert body["extracted_data"]["vendor"] == "Bistro 19"

    decisions = body["decisions"]
    assert decisions, "no decision rounds recorded"
    first = decisions[0]
    assert first["final_route"] == "auto_approve"
    assert first["decided_by"] == "router"
    assert first["agent_recommendation"]
    assert first["agent_confidence"] is not None
    assert first["agent_reasoning"]
    # The ceiling in force at decision time is on the row, not looked up later.
    assert first["ceiling_applied_usd"] is not None
    assert first["retrieved_rule_ids"], "the clauses the analysis saw are not recorded"

    assert len(body["timeline"]) >= 2
    assert body["payment"], "payment outcome missing from the trail"
