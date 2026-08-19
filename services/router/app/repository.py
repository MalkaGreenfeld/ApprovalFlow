"""Decision records, and the reports built from them.

router.decisions carries three requirements:

* F9: one row per decision round, holding the agent's recommendation, confidence
  and reasoning, the clauses it retrieved, the rules the router applied, and who
  made the final call.
* F8: the controller dashboard is aggregation over these rows.
* F10: the ceiling proof is one query over the auto-approved rows, comparing the
  amount enforced against the ceiling that was in force at the time of the
  decision. That is why the ceiling is stored on the row instead of looked up
  afterwards.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import asyncpg

from approvalflow.db import json_safe
from approvalflow.db import row_to_dict as shared_row_to_dict
from approvalflow.logging import get_logger

logger = get_logger(__name__)


#: The decision INSERT itself lives in ``main._persist_and_enqueue`` because it
#: must share a transaction with the outbox write. Everything here is read-side
#: or history.


async def latest_decision(
    pool: asyncpg.Pool, correlation_id: str
) -> dict[str, Any] | None:
    """The most recent decision row for an item."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM router.decisions
            WHERE correlation_id = $1::uuid
            ORDER BY decided_at DESC, revision DESC
            LIMIT 1
            """,
            correlation_id,
        )
    return _row_to_dict(row) if row else None


async def decision_trail(pool: asyncpg.Pool, correlation_id: str) -> list[dict[str, Any]]:
    """Every decision round for an item, oldest first (F9)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM router.decisions
            WHERE correlation_id = $1::uuid
            ORDER BY decided_at, revision
            """,
            correlation_id,
        )
    return [_row_to_dict(r) for r in rows]


async def human_decision_exists(
    pool: asyncpg.Pool, correlation_id: str, revision: int
) -> bool:
    """Whether a human already ruled on this round.

    Guards the approve/reject path against double-submission and against a
    redelivered ``approval.action_received`` event.
    """
    async with pool.acquire() as conn:
        return bool(
            await conn.fetchval(
                """
                SELECT 1 FROM router.decisions
                WHERE correlation_id = $1::uuid AND revision = $2
                  AND decided_by <> 'router'
                LIMIT 1
                """,
                correlation_id,
                revision,
            )
        )


# ── Reports ─────────────────────────────────────────────────────────────────


async def dashboard(pool: asyncpg.Pool, window_hours: int = 24) -> dict[str, Any]:
    """Throughput, autonomy rates and money split (F8).

    'Auto' means ``decided_by = 'router'`` **and** route ``auto_approve``, the
    only path where no human was involved. Everything else is counted as human
    or as a deterministic rejection, so the auto-approval rate cannot be inflated
    by counting router-side rejections as automation.
    """
    async with pool.acquire() as conn:
        totals = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                                                   AS decisions,
                COUNT(DISTINCT correlation_id)                             AS items,
                COUNT(*) FILTER (WHERE final_route = 'auto_approve')       AS auto_approved,
                COUNT(*) FILTER (WHERE final_route = 'human_review')       AS escalated,
                COUNT(*) FILTER (WHERE final_route = 'reject')             AS rejected,
                COUNT(*) FILTER (WHERE final_route = 'duplicate')          AS duplicates,
                COUNT(*) FILTER (WHERE final_route = 'human_approved')     AS human_approved,
                COUNT(*) FILTER (WHERE final_route = 'human_rejected')     AS human_rejected,
                COUNT(*) FILTER (WHERE final_route = 'info_requested')     AS info_requested,
                COALESCE(SUM(enforced_amount_usd)
                    FILTER (WHERE final_route = 'auto_approve'), 0)        AS money_auto,
                COALESCE(SUM(enforced_amount_usd)
                    FILTER (WHERE final_route = 'human_approved'), 0)      AS money_human,
                AVG(agent_confidence) FILTER (WHERE agent_confidence IS NOT NULL)
                                                                           AS avg_confidence
            FROM router.decisions
            WHERE decided_at >= NOW() - ($1 || ' hours')::interval
            """,
            str(window_hours),
        )
        by_rule = await conn.fetch(
            """
            SELECT rule_id, COUNT(*) AS n
            FROM router.decisions, UNNEST(rule_ids_applied) AS rule_id
            WHERE decided_at >= NOW() - ($1 || ' hours')::interval
            GROUP BY rule_id
            ORDER BY n DESC
            LIMIT 10
            """,
            str(window_hours),
        )
        per_hour = await conn.fetch(
            """
            SELECT date_trunc('hour', decided_at) AS hour,
                   COUNT(*) AS decisions,
                   COUNT(*) FILTER (WHERE final_route = 'auto_approve') AS auto_approved
            FROM router.decisions
            WHERE decided_at >= NOW() - ($1 || ' hours')::interval
            GROUP BY hour
            ORDER BY hour
            """,
            str(window_hours),
        )
        latency = await conn.fetchval(
            """
            SELECT AVG(EXTRACT(EPOCH FROM (d.decided_at - s.created_at)))
            FROM router.decisions d
            JOIN ingestion.submissions s USING (correlation_id)
            WHERE d.decided_by = 'router'
              AND d.decided_at >= NOW() - ($1 || ' hours')::interval
            """,
            str(window_hours),
        )

    router_rounds = (
        int(totals["auto_approved"])
        + int(totals["escalated"])
        + int(totals["rejected"])
        + int(totals["duplicates"])
    )
    auto = int(totals["auto_approved"])
    escalated = int(totals["escalated"])
    decidable = auto + escalated

    return {
        "window_hours": window_hours,
        "items": int(totals["items"]),
        "decisions": int(totals["decisions"]),
        "routes": {
            "auto_approve": auto,
            "human_review": escalated,
            "reject": int(totals["rejected"]),
            "duplicate": int(totals["duplicates"]),
            "human_approved": int(totals["human_approved"]),
            "human_rejected": int(totals["human_rejected"]),
            "info_requested": int(totals["info_requested"]),
        },
        "rates": {
            # Share of items the system handled with no human at all.
            "auto_approval_rate": round(auto / decidable, 4) if decidable else 0.0,
            "escalation_rate": round(escalated / decidable, 4) if decidable else 0.0,
            "router_rounds": router_rounds,
        },
        "money_usd": {
            "auto_approved": str(totals["money_auto"] or Decimal("0")),
            "human_approved": str(totals["money_human"] or Decimal("0")),
        },
        "avg_agent_confidence": (
            round(float(totals["avg_confidence"]), 3)
            if totals["avg_confidence"] is not None
            else None
        ),
        "avg_decision_latency_seconds": (
            round(float(latency), 3) if latency is not None else None
        ),
        "top_rules": [{"rule_id": r["rule_id"], "count": int(r["n"])} for r in by_rule],
        "throughput_per_hour": [
            {
                "hour": r["hour"].isoformat(),
                "decisions": int(r["decisions"]),
                "auto_approved": int(r["auto_approved"]),
            }
            for r in per_hour
        ],
    }


async def ceiling_proof(pool: asyncpg.Pool) -> dict[str, Any]:
    """Evidence for F10: no item was ever auto-approved above its ceiling.

    Checks every autonomous approval ever recorded against the ceiling stored on
    that row, so a later configuration change cannot retroactively make a past
    decision look compliant (or non-compliant).
    """
    async with pool.acquire() as conn:
        summary = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                                       AS auto_approvals,
                COALESCE(MAX(enforced_amount_usd), 0)          AS max_amount,
                COALESCE(MAX(ceiling_applied_usd), 0)          AS max_ceiling,
                COUNT(*) FILTER (WHERE enforced_amount_usd > ceiling_applied_usd)
                                                               AS violations,
                COUNT(*) FILTER (WHERE agent_confidence < confidence_threshold)
                                                               AS confidence_violations,
                COUNT(*) FILTER (WHERE decided_by <> 'router')  AS non_router_auto
            FROM router.decisions
            WHERE final_route = 'auto_approve'
            """
        )
        offenders = await conn.fetch(
            """
            SELECT correlation_id, enforced_amount_usd, ceiling_applied_usd,
                   enforced_category, policy_config_version, decided_at
            FROM router.decisions
            WHERE final_route = 'auto_approve'
              AND enforced_amount_usd > ceiling_applied_usd
            ORDER BY decided_at DESC
            LIMIT 50
            """
        )
        by_category = await conn.fetch(
            """
            SELECT enforced_category,
                   COUNT(*) AS n,
                   MAX(enforced_amount_usd) AS max_amount,
                   MIN(ceiling_applied_usd) AS min_ceiling
            FROM router.decisions
            WHERE final_route = 'auto_approve'
            GROUP BY enforced_category
            ORDER BY enforced_category
            """
        )

    violations = int(summary["violations"])
    return {
        "auto_approvals_examined": int(summary["auto_approvals"]),
        "ceiling_violations": violations,
        "confidence_violations": int(summary["confidence_violations"]),
        "auto_approvals_not_made_by_router": int(summary["non_router_auto"]),
        "max_auto_approved_amount_usd": str(summary["max_amount"]),
        "highest_ceiling_in_use_usd": str(summary["max_ceiling"]),
        "per_category": [
            {
                "category": r["enforced_category"],
                "auto_approvals": int(r["n"]),
                "max_amount_usd": str(r["max_amount"]),
                "lowest_ceiling_usd": str(r["min_ceiling"]),
            }
            for r in by_category
        ],
        "offending_items": [
            {
                "correlation_id": str(r["correlation_id"]),
                "amount_usd": str(r["enforced_amount_usd"]),
                "ceiling_usd": str(r["ceiling_applied_usd"]),
                "category": r["enforced_category"],
                "policy_config_version": int(r["policy_config_version"]),
                "decided_at": r["decided_at"].isoformat(),
            }
            for r in offenders
        ],
        "holds": violations == 0 and int(summary["confidence_violations"]) == 0,
    }


# ── Policy version history ──────────────────────────────────────────────────


async def record_policy_version(
    pool: asyncpg.Pool, version: int, document: dict[str, Any], updated_by: str
) -> None:
    """Append an accepted configuration version to the history."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO router.policy_versions (version, document, updated_by)
            VALUES ($1, $2::jsonb, $3)
            ON CONFLICT (version) DO NOTHING
            """,
            version,
            json_safe(document),
            updated_by,
        )


async def list_policy_versions(pool: asyncpg.Pool, limit: int = 25) -> list[dict[str, Any]]:
    """Configuration change history, who changed the posture, and when (F7)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT version, updated_by, updated_at,
                   document -> 'autonomy' AS autonomy
            FROM router.policy_versions
            ORDER BY version DESC
            LIMIT $1
            """,
            limit,
        )
    return [
        {
            "version": int(r["version"]),
            "updated_by": r["updated_by"],
            "updated_at": r["updated_at"].isoformat(),
            "autonomy": r["autonomy"],
        }
        for r in rows
    ]


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    """Convert a decision record to JSON-safe primitives (money as strings)."""
    return shared_row_to_dict(row, ("agent_violations",))
