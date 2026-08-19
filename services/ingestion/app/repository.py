"""Submissions, the outbox, and the status timeline.

Ingestion is the single writer of submissions.status; notification owns the
approver queue. One writer per field keeps the final value from depending on
which subscriber won a race, which is what lets the status timeline serve as an
audit source (F9).
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import asyncpg

from approvalflow.db import json_safe
from approvalflow.db import row_to_dict as shared_row_to_dict
from approvalflow.logging import get_logger

logger = get_logger(__name__)

#: Once a submission reaches one of these it must not move again. A redelivered
#: event or a late-arriving handler cannot resurrect a paid or rejected item.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"paid", "rejected", "human_rejected", "duplicate", "compensated"}
)


async def find_prior_submission(
    conn: asyncpg.Connection, vendor: str, invoice_number: str, total: Decimal
) -> asyncpg.Record | None:
    """Find an earlier live submission with the same business key.

    "Live" excludes anything that ended without a payment: a genuinely rejected
    or compensated invoice may legitimately be re-submitted, while one that is in
    flight or already paid may not (F3 / GLOBAL-DUP).

    Must be called inside the advisory-locked transaction from
    :func:`lock_business_key`, otherwise two simultaneous copies of the same
    invoice can both find nothing.
    """
    return await conn.fetchrow(
        """
        SELECT correlation_id, status
        FROM ingestion.submissions
        WHERE vendor = $1 AND invoice_number = $2 AND total = $3
          AND status <> ALL($4::text[])
        ORDER BY created_at
        LIMIT 1
        """,
        vendor,
        invoice_number,
        total,
        ["rejected", "human_rejected", "duplicate", "compensated", "payment_failed"],
    )


async def lock_business_key(conn: asyncpg.Connection, idempotency_key: str) -> None:
    """Serialise duplicate detection for one business key.

    A transaction-scoped advisory lock is cheaper than table locking and released
    automatically on commit or rollback. Without it, two identical submissions
    arriving in the same millisecond would both pass the duplicate check.
    """
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", idempotency_key)


async def insert_submission(
    conn: asyncpg.Connection, data: dict[str, Any]
) -> None:
    """Insert the submission row (inside the caller's transaction)."""
    await conn.execute(
        """
        INSERT INTO ingestion.submissions (
            correlation_id, submitter_email, department, vendor, vendor_known,
            invoice_number, currency, amount_original, amount_usd, category,
            attendees, line_items, tax_amount, total, receipt_present, math_ok,
            raw_payload, notes, status, idempotency_key, duplicate_of
        ) VALUES (
            $1::uuid, $2, $3, $4, $5,
            $6, $7, $8, $9, $10,
            $11, $12::jsonb, $13, $14, $15, $16,
            $17::jsonb, $18, $19, $20, $21::uuid
        )
        """,
        data["correlation_id"],
        data["submitter_email"],
        data["department"],
        data["vendor"],
        data["vendor_known"],
        data["invoice_number"],
        data["currency"],
        data["amount_original"],
        data["amount_usd"],
        data["category"],
        data["attendees"],
        json_safe(data["line_items"]),
        data["tax_amount"],
        data["total"],
        data["receipt_present"],
        data["math_ok"],
        json_safe(data["raw_payload"]),
        data["notes"],
        data["status"],
        data["idempotency_key"],
        data["duplicate_of"],
    )


async def record_event(
    conn: asyncpg.Connection,
    *,
    correlation_id: str,
    event_type: str,
    from_status: str | None = None,
    to_status: str | None = None,
    actor: str = "system",
    detail: dict[str, Any] | None = None,
) -> None:
    """Append to the append-only timeline (never updated, only inserted)."""
    await conn.execute(
        """
        INSERT INTO ingestion.submission_events
            (correlation_id, event_type, from_status, to_status, actor, detail)
        VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb)
        """,
        correlation_id,
        event_type,
        from_status,
        to_status,
        actor,
        json_safe(detail or {}),
    )


async def get_submission(pool: asyncpg.Pool, correlation_id: str) -> dict[str, Any] | None:
    """Fetch one submission as JSON-safe primitives."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM ingestion.submissions WHERE correlation_id = $1::uuid",
            correlation_id,
        )
    return row_to_dict(row) if row else None


async def update_status(
    pool: asyncpg.Pool,
    correlation_id: str,
    new_status: str,
    *,
    event_type: str,
    actor: str = "system",
    detail: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    """Move a submission to ``new_status`` and record it on the timeline.

    Returns:
        ``(changed, previous_status)``. ``changed`` is ``False`` when the row is
        missing, already in that status (redelivered event) or already terminal ,
        all three are normal, none is an error.
    """
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
                SELECT status FROM ingestion.submissions
                WHERE correlation_id = $1::uuid
                FOR UPDATE
                """,
            correlation_id,
        )
        if row is None:
            logger.warning(
                "Status update for unknown submission (status=%s)", new_status
            )
            return False, None

        current = str(row["status"])
        if current == new_status:
            return False, current
        if current in TERMINAL_STATUSES:
            logger.info(
                "Ignoring %s -> %s: %s is terminal",
                current, new_status, current,
            )
            return False, current

        await conn.execute(
            """
                UPDATE ingestion.submissions
                SET status = $2, updated_at = NOW()
                WHERE correlation_id = $1::uuid
                """,
            correlation_id,
            new_status,
        )
        await record_event(
            conn,
            correlation_id=correlation_id,
            event_type=event_type,
            from_status=current,
            to_status=new_status,
            actor=actor,
            detail=detail,
        )
        return True, current


async def open_info_request(
    pool: asyncpg.Pool,
    correlation_id: str,
    request: dict[str, Any],
) -> bool:
    """Attach the approver's "what we need from you" box to the submission (F5).

    This is what turns a bare ``info_requested`` status into something the
    submitter can act on: the requested fields and the question are shown on the
    tracking screen.
    """
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE ingestion.submissions
            SET open_info_request = $2::jsonb, updated_at = NOW()
            WHERE correlation_id = $1::uuid
            """,
            correlation_id,
            json_safe(request),
        )
    return result.endswith("1")


async def apply_info_response(
    conn: asyncpg.Connection,
    correlation_id: str,
    *,
    updates: dict[str, Any],
    answer: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    """Apply a submitter's answer, bump the revision and clear the open request.

    Runs inside the caller's transaction together with the outbox write, so the
    amended submission and the ``submission.info_provided`` event are atomic.

    Args:
        updates: Already-whitelisted column values to change.
        answer: The full answer payload appended to ``info_exchange``.
        actor: Who answered.

    Returns:
        The amended row.

    Raises:
        LookupError: if the submission does not exist.
        ValueError: if it has no open info request (nothing to answer).
    """
    row = await conn.fetchrow(
        """
        SELECT * FROM ingestion.submissions
        WHERE correlation_id = $1::uuid
        FOR UPDATE
        """,
        correlation_id,
    )
    if row is None:
        raise LookupError(f"submission {correlation_id} not found")
    if row["open_info_request"] is None:
        raise ValueError("no open information request for this submission")

    open_request = row["open_info_request"]
    if isinstance(open_request, str):
        open_request = json.loads(open_request)

    exchange_entry = {
        "revision": int(row["revision"]),
        "request": open_request,
        "answer": json_safe(answer),
        "answered_by": actor,
    }

    # Build the SET clause from the whitelisted updates only.
    assignments = ["status = 'reanalyzing'", "revision = revision + 1",
                   "open_info_request = NULL", "updated_at = NOW()",
                   "info_exchange = info_exchange || $2::jsonb"]
    params: list[Any] = [correlation_id, [exchange_entry]]
    for column, value in updates.items():
        params.append(value)
        placeholder = f"${len(params)}"
        cast = "::jsonb" if column == "line_items" else ""
        assignments.append(f"{column} = {placeholder}{cast}")

    amended = await conn.fetchrow(
        f"""
        UPDATE ingestion.submissions
        SET {", ".join(assignments)}
        WHERE correlation_id = $1::uuid
        RETURNING *
        """,
        *params,
    )
    await record_event(
        conn,
        correlation_id=correlation_id,
        event_type="info_provided",
        from_status=str(row["status"]),
        to_status="reanalyzing",
        actor=actor,
        detail={"fields_changed": sorted(updates.keys()), "answer": json_safe(answer)},
    )
    assert amended is not None
    return row_to_dict(amended)


async def timeline(pool: asyncpg.Pool, correlation_id: str) -> list[dict[str, Any]]:
    """The append-only status timeline for one item (F9)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT event_type, from_status, to_status, actor, detail, occurred_at
            FROM ingestion.submission_events
            WHERE correlation_id = $1::uuid
            ORDER BY occurred_at, id
            """,
            correlation_id,
        )
    return [
        {
            "event_type": r["event_type"],
            "from_status": r["from_status"],
            "to_status": r["to_status"],
            "actor": r["actor"],
            "detail": r["detail"],
            "occurred_at": r["occurred_at"].isoformat(),
        }
        for r in rows
    ]


async def replay_response(
    conn: asyncpg.Connection, idempotency_key: str
) -> dict[str, Any] | None:
    """Return the stored response for a repeated ``Idempotency-Key`` (M10)."""
    row = await conn.fetchrow(
        """
        SELECT response_body FROM ingestion.request_idempotency
        WHERE idempotency_key = $1
        """,
        idempotency_key,
    )
    if row is None:
        return None
    body = row["response_body"]
    return json.loads(body) if isinstance(body, str) else dict(body)


async def remember_response(
    conn: asyncpg.Connection,
    idempotency_key: str,
    correlation_id: str,
    response_body: dict[str, Any],
) -> None:
    """Store the response so a client retry replays it instead of re-submitting."""
    await conn.execute(
        """
        INSERT INTO ingestion.request_idempotency
            (idempotency_key, correlation_id, response_body)
        VALUES ($1, $2::uuid, $3::jsonb)
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        idempotency_key,
        correlation_id,
        json_safe(response_body),
    )


async def throughput(pool: asyncpg.Pool, window_hours: int = 24) -> dict[str, Any]:
    """Submission-side counters for the controller dashboard (F8)."""
    async with pool.acquire() as conn:
        totals = await conn.fetchrow(
            """
            SELECT COUNT(*) AS submitted,
                   COUNT(*) FILTER (WHERE duplicate_of IS NOT NULL) AS duplicates,
                   COUNT(*) FILTER (WHERE status = 'paid') AS paid,
                   COUNT(*) FILTER (WHERE status IN ('human_review','info_requested'))
                        AS awaiting_human,
                   COALESCE(SUM(amount_usd) FILTER (WHERE status = 'paid'), 0) AS paid_usd
            FROM ingestion.submissions
            WHERE created_at >= NOW() - ($1 || ' hours')::interval
            """,
            str(window_hours),
        )
        by_status = await conn.fetch(
            """
            SELECT status, COUNT(*) AS n
            FROM ingestion.submissions
            WHERE created_at >= NOW() - ($1 || ' hours')::interval
            GROUP BY status ORDER BY n DESC
            """,
            str(window_hours),
        )
    return {
        "window_hours": window_hours,
        "submitted": int(totals["submitted"]),
        "duplicates_short_circuited": int(totals["duplicates"]),
        "paid": int(totals["paid"]),
        "awaiting_human": int(totals["awaiting_human"]),
        "paid_usd": str(totals["paid_usd"]),
        "by_status": {r["status"]: int(r["n"]) for r in by_status},
    }


#: Columns held as JSONB on the submissions table.
JSON_COLUMNS = ("line_items", "raw_payload", "info_exchange", "open_info_request")


def row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    """Convert an asyncpg record into JSON-safe primitives.

    Thin wrapper over :func:`approvalflow.db.row_to_dict` so every service
    serialises money the same way (``Decimal`` as a string, never a float).
    """
    return shared_row_to_dict(row, JSON_COLUMNS)
