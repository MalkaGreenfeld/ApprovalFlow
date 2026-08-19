"""Shared Pydantic models for ApprovalFlow, single source of truth
for all event schemas and data structures flowing between services.

Every service imports from this package so the contract is explicit.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

# ── Enums ──────────────────────────────────────────────────────────


class SubmissionStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    AUTO_APPROVED = "auto_approved"
    HUMAN_REVIEW = "human_review"
    HUMAN_APPROVED = "human_approved"
    HUMAN_REJECTED = "human_rejected"
    INFO_REQUESTED = "info_requested"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    PAID = "paid"
    PAYMENT_FAILED = "payment_failed"
    COMPENSATED = "compensated"


class DecisionRoute(StrEnum):
    AUTO_APPROVE = "auto_approve"
    HUMAN_REVIEW = "human_review"
    REJECT = "reject"
    DUPLICATE = "duplicate"


class SagaState(StrEnum):
    STARTED = "started"
    BUDGET_RESERVED = "budget_reserved"
    PAYMENT_EXECUTED = "payment_executed"
    CONFIRMED = "confirmed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    PAYMENT_FAILED = "payment_failed"


# ── Line Item ──────────────────────────────────────────────────────


class LineItem(BaseModel):
    description: str
    quantity: int = 1
    unit_price: Decimal = Field(alias="unitPrice")

    @property
    def total(self) -> Decimal:
        return self.unit_price * self.quantity


# ── Submission (incoming request) ──────────────────────────────────


class SubmissionRequest(BaseModel):
    """Incoming payload for POST /api/submissions."""

    submitter: str
    department: str
    vendor: str
    vendorKnown: bool = False
    invoiceNumber: str
    currency: str = "USD"
    category: str
    attendees: int | None = None
    lineItems: list[LineItem]
    taxAmount: Decimal = Field(default=Decimal("0"))
    total: Decimal
    receiptPresent: bool = False
    date: str | None = None
    notes: str | None = None


class Submission(BaseModel):
    """Full submission record as stored in PostgreSQL."""

    id: UUID = Field(default_factory=uuid4)
    correlation_id: UUID = Field(alias="correlationId")
    submitter_email: str
    department: str
    vendor: str
    vendor_known: bool = False
    invoice_number: str
    currency: str = "USD"
    amount_original: Decimal
    amount_usd: Decimal
    category: str
    attendees: int | None = None
    line_items: list[LineItem] = Field(default_factory=list)
    tax_amount: Decimal = Decimal("0")
    total: Decimal
    receipt_present: bool = False
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None
    status: SubmissionStatus = SubmissionStatus.RECEIVED
    idempotency_key: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)


# ── Agent Output ───────────────────────────────────────────────────


class PolicyViolation(BaseModel):
    rule_id: str
    description: str


class AgentOutput(BaseModel):
    """Structured output from the AI agent, recommends, never decides."""

    recommendation: DecisionRoute
    confidence: float
    violations: list[PolicyViolation] = Field(default_factory=list)
    reasoning: str = ""
    amount_usd: Decimal
    category: str


# ── Router Decision ────────────────────────────────────────────────


class Decision(BaseModel):
    """The deterministic router decision, the single source of truth."""

    id: UUID = Field(default_factory=uuid4)
    correlation_id: UUID
    agent_recommendation: DecisionRoute | None = None
    agent_confidence: float | None = None
    agent_violations: list[PolicyViolation] = Field(default_factory=list)
    agent_reasoning: str | None = None
    rule_ids_applied: list[str] = Field(default_factory=list)
    final_route: DecisionRoute
    rejection_reason: str | None = None
    decided_by: str = "router"  # "router" or approver_email
    decided_at: datetime = Field(default_factory=datetime.now)


# ── Events ─────────────────────────────────────────────────────────


class CloudEvent(BaseModel):
    """Minimal CloudEvents v1 envelope used by Dapr pub/sub."""

    type: str
    source: str
    id: str
    correlation_id: UUID
    time: datetime = Field(default_factory=datetime.now)
    data: dict[str, Any] = Field(default_factory=dict)
