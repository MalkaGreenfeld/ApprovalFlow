"""Payment persistence: seed budgets in, saga audit out.

The live budget counter is a Dapr state key, because that is where the ETag
concurrency control lives. PostgreSQL holds the parts SQL does better:

* payment.department_budgets, the starting figures, so budgets are data in the
  database rather than a dict compiled into the service;
* payment.payment_attempts, one row per saga run with its step-by-step outcome
  including compensations, for the audit trail (F9 / M9).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import asyncpg

from approvalflow.db import json_safe
from approvalflow.db import row_to_dict as shared_row_to_dict
from approvalflow.logging import get_logger

logger = get_logger(__name__)


async def seed_budgets(pool: asyncpg.Pool) -> dict[str, Decimal]:
    """The configured starting budget per department."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT department, remaining FROM payment.department_budgets"
        )
    return {r["department"]: Decimal(str(r["remaining"])) for r in rows}


async def record_attempt(
    pool: asyncpg.Pool,
    *,
    correlation_id: str,
    saga_id: str,
    department: str,
    amount_usd: Decimal,
    outcome: str,
    final_saga_state: str,
    steps: list[dict[str, Any]],
    error_message: str | None,
) -> None:
    """Write the terminal audit record for one saga run.

    Unique on ``(correlation_id, saga_id)`` so a retried handler cannot produce a
    second audit row for the same run.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO payment.payment_attempts (
                correlation_id, saga_id, department, amount_usd,
                outcome, final_saga_state, steps, error_message
            ) VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7::jsonb, $8)
            ON CONFLICT (correlation_id, saga_id) DO NOTHING
            """,
            correlation_id,
            saga_id,
            department,
            amount_usd,
            outcome,
            final_saga_state,
            json_safe(steps),
            error_message,
        )


async def attempts(pool: asyncpg.Pool, correlation_id: str) -> list[dict[str, Any]]:
    """Every saga run recorded for one item."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT saga_id, department, amount_usd, outcome, final_saga_state,
                   steps, error_message, created_at
            FROM payment.payment_attempts
            WHERE correlation_id = $1::uuid
            ORDER BY created_at
            """,
            correlation_id,
        )
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    """JSON-safe view of a saga or reservation row."""
    return shared_row_to_dict(row, ("steps",))
