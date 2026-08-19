"""Shared fixtures for the ApprovalFlow test suite.

Unit tests must run with no Docker, no Dapr sidecar and no database, so the
fixtures here provide the bootstrap policy configuration and an in-memory stand-in
for the Dapr state store that implements the same ETag semantics as the real one.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

# Tokens are not part of the unit-test surface; the auth dependency is covered by
# its own tests and by the verification script end to end.
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("LLM_PROVIDER", "stub")


@pytest.fixture
def policy_config():
    """The bootstrap policy configuration, loaded from config/policy-config.json."""
    from approvalflow.policy import default_policy_config

    return default_policy_config()


@pytest.fixture
def valid_submission() -> dict:
    """An in-policy submission that should auto-approve (INV-1001 style)."""
    # Deliberately *without* the ``submitted_amount_usd`` / ``submitted_category``
    # twins: the router falls back to ``amount_usd`` / ``category`` when they are
    # absent, so a gate test can change the amount in one place. The tests that
    # exercise the submitted-versus-agent disagreement set both explicitly.
    return {
        "correlation_id": "11111111-1111-4111-8111-111111111111",
        "amount_usd": 42.0,
        "category": "meals",
        "currency": "USD",
        "vendor_known": True,
        "receipt_present": True,
        "math_ok": True,
        "vendor": "Bistro 19",
        "invoice_number": "NW-INV-7781",
        "total": 42.0,
        "line_items": [
            {"description": "Team lunch", "quantity": 1, "unitPrice": 38.89}
        ],
        "attendees": 1,
        "notes": "Team lunch",
    }


@pytest.fixture
def valid_agent_output() -> dict:
    """An agent recommendation of auto_approve with high confidence."""
    return {
        "recommendation": "auto_approve",
        "confidence": 0.95,
        "violations": [],
        "reasoning": "Routine team lunch, all policies satisfied",
        "amount_usd": 42.0,
        "category": "meals",
    }


class FakeStateClient:
    """In-memory Dapr state store with real ETag behaviour.

    Subclasses the production client so ``update_atomic`` under test is the actual
    compare-and-set loop, not a reimplementation of it.
    """

    def __init__(self) -> None:
        from approvalflow.dapr_client import DaprStateClient

        self._values: dict[str, dict[str, Any]] = {}
        self._etags: dict[str, int] = {}
        #: Set to a positive number to make the next N conditional saves lose the
        #: ETag race, simulating a competing writer.
        self.conflicts_to_inject = 0
        self._real = DaprStateClient

    async def get(self, key: str) -> dict[str, Any] | None:
        value = self._values.get(key)
        return dict(value) if value is not None else None

    async def get_with_etag(self, key: str) -> tuple[dict[str, Any] | None, str | None]:
        value = self._values.get(key)
        if value is None:
            return None, None
        return dict(value), str(self._etags.get(key, 1))

    async def save(
        self, key: str, value: dict[str, Any], etag: str | None = None
    ) -> bool:
        # Only a *conditional* write can lose an ETag race, which is what the
        # real state store does: an unconditional save is last-write-wins and
        # never returns 409. Injecting conflicts into unconditional saves as well
        # made the saga look as though it could not even record itself.
        if etag is not None and self.conflicts_to_inject > 0:
            self.conflicts_to_inject -= 1
            # A competing writer bumped the version behind our back.
            self._etags[key] = self._etags.get(key, 1) + 1
            return False
        if etag is not None and str(self._etags.get(key, 1)) != str(etag):
            return False
        self._values[key] = dict(value)
        self._etags[key] = self._etags.get(key, 1) + 1
        return True

    async def delete(self, key: str) -> bool:
        self._values.pop(key, None)
        self._etags.pop(key, None)
        return True

    async def update_atomic(self, key, mutate, *, retries: int = 5):
        from approvalflow.dapr_client import DaprStateClient

        return await DaprStateClient.update_atomic(self, key, mutate, retries=retries)


@pytest.fixture
def fake_state() -> FakeStateClient:
    return FakeStateClient()
