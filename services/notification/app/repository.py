"""The approver queue and the information requests.

Both are indexed tables rather than state-store keys, so "show me only what was
escalated" is one query with the filtering done in the database (F4). A system
meant to serve millions of users cannot list a queue with one read per item.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from approvalflow.db import json_safe
from approvalflow.db import row_to_dict as shared_row_to_dict
from approvalflow.logging import get_logger

logger = get_logger(__name__)


async def upsert_queue_item(pool: asyncpg.Pool, item: dict[str, Any]) -> None:
    """Create or refresh the queue entry for an escalated item.

    Called on every ``decision.escalated``, including the escalation that follows
    a re-analysis, so the approver sees the newest rationale and revision rather
    than the first one.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO notification.approval_queue (
                correlation_id, status, revision, vendor, submitter_email,
                department, category, amount_usd, agent_recommendation,
                agent_confidence, agent_reasoning, rule_ids, escalation_reason,
                paused_at
            ) VALUES (
                $1::uuid, 'pending', $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11::text[], $12, NOW()
            )
            ON CONFLICT (correlation_id) DO UPDATE SET
                status = 'pending',
                revision = EXCLUDED.revision,
                amount_usd = EXCLUDED.amount_usd,
                category = EXCLUDED.category,
                agent_recommendation = EXCLUDED.agent_recommendation,
                agent_confidence = EXCLUDED.agent_confidence,
                agent_reasoning = EXCLUDED.agent_reasoning,
                rule_ids = EXCLUDED.rule_ids,
                escalation_reason = EXCLUDED.escalation_reason,
                resumed_at = NOW(),
                updated_at = NOW()
            """,
            item["correlation_id"],
            int(item.get("revision", 0)),
            item.get("vendor"),
            item.get("submitter_email"),
            item.get("department"),
            item.get("category"),
            item.get("amount_usd"),
            item.get("agent_recommendation"),
            item.get("agent_confidence"),
            item.get("agent_reasoning"),
            list(item.get("rule_ids") or []),
            item.get("escalation_reason"),
        )


async def set_queue_status(
    pool: asyncpg.Pool,
    correlation_id: str,
    new_status: str,
    *,
    approver_email: str | None = None,
    action: str | None = None,
    comment: str | None = None,
) -> bool:
    """Move a queue entry's status. Returns ``False`` if there is no such entry."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE notification.approval_queue
            SET status = $2,
                approver_email = COALESCE($3, approver_email),
                approver_action = COALESCE($4, approver_action),
                approver_comment = COALESCE($5, approver_comment),
                updated_at = NOW()
            WHERE correlation_id = $1::uuid
            """,
            correlation_id,
            new_status,
            approver_email,
            action,
            comment,
        )
    return result.endswith("1")


async def claim_for_action(
    conn: asyncpg.Connection, correlation_id: str, expected_statuses: tuple[str, ...]
) -> asyncpg.Record | None:
    """Lock a queue row for an approver action.

    ``FOR UPDATE`` plus a status check is what stops two approvers clicking
    Approve at the same moment from producing two decisions (M10). Returns
    ``None`` when the item is missing or no longer actionable.
    """
    return await conn.fetchrow(
        """
        SELECT * FROM notification.approval_queue
        WHERE correlation_id = $1::uuid AND status = ANY($2::text[])
        FOR UPDATE
        """,
        correlation_id,
        list(expected_statuses),
    )


async def list_queue(
    pool: asyncpg.Pool,
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """The approver queue: only escalated items, with the agent's rationale (F4)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT q.*,
                   r.question        AS open_question,
                   r.requested_fields AS open_requested_fields
            FROM notification.approval_queue q
            LEFT JOIN notification.info_requests r
                   ON r.correlation_id = q.correlation_id
                  AND r.status = 'pending'
            WHERE ($1::text IS NULL OR q.status = $1)
            ORDER BY q.created_at DESC
            LIMIT $2 OFFSET $3
            """,
            status,
            limit,
            offset,
        )
    return [_row_to_dict(r) for r in rows]


async def get_queue_item(pool: asyncpg.Pool, correlation_id: str) -> dict[str, Any] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM notification.approval_queue WHERE correlation_id = $1::uuid",
            correlation_id,
        )
    return _row_to_dict(row) if row else None


# ── Information requests (F5) ───────────────────────────────────────────────


async def create_info_request(
    conn: asyncpg.Connection,
    *,
    correlation_id: str,
    revision: int,
    requested_by: str,
    requested_fields: list[str],
    question: str,
) -> None:
    """Record exactly what the approver is asking the submitter for."""
    await conn.execute(
        """
        INSERT INTO notification.info_requests
            (correlation_id, revision, requested_by, requested_fields, question)
        VALUES ($1::uuid, $2, $3, $4::text[], $5)
        ON CONFLICT (correlation_id, revision) DO UPDATE SET
            requested_by = EXCLUDED.requested_by,
            requested_fields = EXCLUDED.requested_fields,
            question = EXCLUDED.question,
            status = 'pending',
            requested_at = NOW()
        """,
        correlation_id,
        revision,
        requested_by,
        list(requested_fields),
        question,
    )


async def close_info_request(
    pool: asyncpg.Pool,
    correlation_id: str,
    *,
    answer: dict[str, Any],
    answered_by: str,
) -> bool:
    """Mark the open request answered when the submitter replies."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE notification.info_requests
            SET status = 'answered', answer = $2::jsonb,
                answered_by = $3, answered_at = NOW()
            WHERE correlation_id = $1::uuid AND status = 'pending'
            """,
            correlation_id,
            json_safe(answer),
            answered_by,
        )
    return result.endswith("1")


async def append_exchange(
    pool: asyncpg.Pool, correlation_id: str, exchange: list[dict[str, Any]]
) -> None:
    """Store the question/answer thread on the queue entry for the approver."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE notification.approval_queue
            SET info_exchange = $2::jsonb, updated_at = NOW()
            WHERE correlation_id = $1::uuid
            """,
            correlation_id,
            json_safe(exchange),
        )


async def info_requests(pool: asyncpg.Pool, correlation_id: str) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM notification.info_requests
            WHERE correlation_id = $1::uuid
            ORDER BY requested_at
            """,
            correlation_id,
        )
    return [_row_to_dict(r) for r in rows]


# ── Delivered notifications (M8) ────────────────────────────────────────────


async def record_notification(
    pool: asyncpg.Pool,
    *,
    correlation_id: str,
    recipient: str,
    subject: str,
    body: str,
    event_type: str,
    channel: str = "log",
) -> None:
    """Persist the outbound notification.

    The delivery channel here is the structured log, the assignment requires a
    notification channel, not an SMTP server, but recording it makes "the
    submitter was informed" auditable instead of a claim.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO notification.notifications
                (correlation_id, channel, recipient, subject, body, event_type)
            VALUES ($1::uuid, $2, $3, $4, $5, $6)
            """,
            correlation_id,
            channel,
            recipient or "unknown",
            subject,
            body,
            event_type,
        )


async def notifications(pool: asyncpg.Pool, correlation_id: str) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT channel, recipient, subject, body, event_type, sent_at
            FROM notification.notifications
            WHERE correlation_id = $1::uuid
            ORDER BY sent_at
            """,
            correlation_id,
        )
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    """JSON-safe view of an approval-queue or info-request row."""
    return shared_row_to_dict(row, ("info_exchange", "answer"))
