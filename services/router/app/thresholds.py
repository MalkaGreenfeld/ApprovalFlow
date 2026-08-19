"""Deprecated. Thresholds are not hard-coded here any more.

The tier ceilings, the confidence bar and the hard-stop list live in
config/policy-config.json, served from the Dapr state store and editable through
PUT /api/admin/policy with no redeploy (F7 / M13).

The only thresholds still in code are the bounds configuration may not cross,
next to the loader that enforces them: approvalflow.policy.ABSOLUTE_MAX_CEILING_USD
and approvalflow.policy.MIN_CONFIDENCE_THRESHOLD.

Kept so an older import fails with an explanation instead of an ImportError.
"""

from __future__ import annotations

from approvalflow.policy import (
    ABSOLUTE_MAX_CEILING_USD,
    MIN_CONFIDENCE_THRESHOLD,
    default_policy_config,
)

__all__ = [
    "ABSOLUTE_MAX_CEILING_USD",
    "MIN_CONFIDENCE_THRESHOLD",
    "current_ceilings",
    "current_confidence_bar",
]


def current_ceilings() -> dict[str, str]:
    """Ceilings from the bootstrap config, for diagnostics only.

    Runtime code must read the *live* config through ``PolicyConfigStore`` so it
    sees an administrator's change without a restart.
    """
    config = default_policy_config()
    return {
        category: str(ceiling)
        for category, ceiling in config.autonomy.category_ceilings_usd.items()
    }


def current_confidence_bar() -> str:
    """Confidence bar from the bootstrap config, for diagnostics only."""
    return str(default_policy_config().autonomy.confidence_threshold)
