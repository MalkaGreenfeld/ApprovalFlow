"""Event type constants shared across all services.

Each event type maps to a Dapr pub/sub topic. Services publish and
subscribe using these strings so there's one source of truth.
"""

# ── Ingestion Service publishes ─────────────────────────────────────
SUBMISSION_RECEIVED = "submission.received"
#: A submitter answered an approver's "send back for more info" request, so the
#: paused workflow resumes with the enriched submission (F5 / M11).
SUBMISSION_INFO_PROVIDED = "submission.info_provided"

# ── Agent Service publishes ─────────────────────────────────────────
AGENT_ANALYZED = "agent.analyzed"

# ── Router Service publishes ────────────────────────────────────────
DECISION_AUTO_APPROVED = "decision.auto_approved"
DECISION_ESCALATED = "decision.escalated"
DECISION_REJECTED = "decision.rejected"
DECISION_DUPLICATE = "decision.duplicate"
DECISION_HUMAN_APPROVED = "decision.human_approved"
DECISION_HUMAN_REJECTED = "decision.human_rejected"
DECISION_INFO_REQUESTED = "decision.info_requested"

# ── Payment Service publishes ──────────────────────────────────────
PAYMENT_COMPLETED = "payment.completed"
PAYMENT_FAILED = "payment.failed"
PAYMENT_COMPENSATED = "payment.compensated"

# ── Notification Service publishes ──────────────────────────────────
APPROVAL_ACTION_RECEIVED = "approval.action_received"

#: Every topic, used by the tests to assert the declarative Dapr subscriptions
#: and the handler routes stay in step with this list.
ALL_TOPICS: tuple[str, ...] = (
    SUBMISSION_RECEIVED,
    SUBMISSION_INFO_PROVIDED,
    AGENT_ANALYZED,
    DECISION_AUTO_APPROVED,
    DECISION_ESCALATED,
    DECISION_REJECTED,
    DECISION_DUPLICATE,
    DECISION_HUMAN_APPROVED,
    DECISION_HUMAN_REJECTED,
    DECISION_INFO_REQUESTED,
    PAYMENT_COMPLETED,
    PAYMENT_FAILED,
    PAYMENT_COMPENSATED,
    APPROVAL_ACTION_RECEIVED,
)
