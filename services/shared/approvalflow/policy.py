"""Externally configurable policy and autonomy thresholds (F7 / M13).

One document holds the FX rates, the per-category autonomy ceilings, the
confidence bar and the rule catalogue.

config/policy-config.json is the bootstrap copy. The live copy lives in the Dapr
state store under ``config:policy``: an admin changes it through
PUT /api/admin/policy, and every replica picks the new version up within
POLICY_CONFIG_TTL_SECONDS (default 5s) with no rebuild or restart.

Configuration can only ever restrict autonomy, never widen it:

* RuleOutcome has no auto_approve member, so no edit can turn an escalation into
  an automatic approval;
* ceilings are clamped by ABSOLUTE_MAX_CEILING_USD and the confidence bar by
  MIN_CONFIDENCE_THRESHOLD, both compiled into this module;
* validation runs on load and on save, and an invalid document is rejected
  instead of ignored (M15).
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .logging import get_logger

logger = get_logger(__name__)

# ── Hard, non-configurable safety bounds (M12) ──────────────────────────────

#: No configuration edit may raise an autonomy ceiling above this.
ABSOLUTE_MAX_CEILING_USD = Decimal("2000")
#: No configuration edit may lower the confidence bar below this.
MIN_CONFIDENCE_THRESHOLD = Decimal("0.50")

#: Dapr state keys holding the live configuration and the live policy prose.
POLICY_CONFIG_KEY = "config:policy"
POLICY_DOCUMENT_KEY = "config:policy-document"

_DEFAULT_CONFIG_PATH = Path(
    os.getenv("POLICY_CONFIG_PATH", "/app/config/policy-config.json")
)
_DEFAULT_DOCUMENT_PATH = Path(os.getenv("POLICY_DOCUMENT_PATH", "/app/policy.md"))
_TTL_SECONDS = float(os.getenv("POLICY_CONFIG_TTL_SECONDS", "5"))


class PolicyConfigError(ValueError):
    """Raised when a policy document is structurally or semantically invalid."""


# ── Rule model ──────────────────────────────────────────────────────────────


class RuleOutcome(StrEnum):
    """Outcomes a configured rule may produce.

    Note the absence of ``auto_approve``: configuration can only ever be more
    conservative than the default. This is what makes the ceiling provably
    unbypassable no matter what an administrator writes.
    """

    HUMAN_REVIEW = "human_review"
    REJECT = "reject"
    DUPLICATE = "duplicate"


class PolicyRule(BaseModel):
    """A single declarative policy rule.

    ``when`` is a predicate tree evaluated by :mod:`approvalflow.ruleengine`
    against the facts built by :mod:`approvalflow.facts`.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1, max_length=64)
    outcome: RuleOutcome = RuleOutcome.HUMAN_REVIEW
    #: Shown to the approver and stored on the decision row.
    reason: str = ""
    #: Free-text note for whoever edits the policy next. Never shown to a user
    #: and never used in a decision, it exists so the reasoning behind a rule
    #: lives next to the rule instead of only in a commit message.
    note: str = ""
    enabled: bool = True
    #: Categories the rule applies to; ``["*"]`` means every category.
    categories: list[str] = Field(default_factory=lambda: ["*"])
    #: Companion rule ids recorded in the audit trail alongside ``rule_id``.
    also_cites: list[str] = Field(default_factory=list)
    when: dict[str, Any]

    @field_validator("categories")
    @classmethod
    def _lowercase_categories(cls, value: list[str]) -> list[str]:
        return [c.strip().lower() for c in value if c.strip()] or ["*"]

    def applies_to(self, category: str) -> bool:
        return "*" in self.categories or category.lower() in self.categories


class AutonomyConfig(BaseModel):
    """The autonomy posture, the dilemma expressed as data."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    about: str = Field(default="", alias="_about")
    confidence_threshold: Decimal = Decimal("0.85")
    default_ceiling_usd: Decimal = Decimal("350")
    category_ceilings_usd: dict[str, Decimal] = Field(default_factory=dict)

    @field_validator("category_ceilings_usd")
    @classmethod
    def _lowercase_keys(cls, value: dict[str, Decimal]) -> dict[str, Decimal]:
        return {k.strip().lower(): v for k, v in value.items()}

    def ceiling_for(self, category: str) -> Decimal:
        """Ceiling for a category, falling back to the configured default."""
        return self.category_ceilings_usd.get(
            category.strip().lower(), self.default_ceiling_usd
        )


class PolicyConfig(BaseModel):
    """The whole externally configurable policy."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    about: str = Field(default="", alias="_about")
    version: int = 1
    updated_by: str = "bootstrap"
    updated_at: str = ""
    base_currency: str = "USD"
    fx_rates: dict[str, Decimal] = Field(default_factory=dict)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    receipt_required_above_usd: Decimal = Decimal("25")
    #: Fields a submitter is allowed to change when answering an info request (F5).
    amendable_fields: list[str] = Field(
        default_factory=lambda: ["notes", "attendees", "receiptPresent"]
    )
    rules: list[PolicyRule] = Field(default_factory=list)

    @field_validator("fx_rates")
    @classmethod
    def _uppercase_currencies(cls, value: dict[str, Decimal]) -> dict[str, Decimal]:
        return {k.strip().upper(): v for k, v in value.items()}

    # -- validation ------------------------------------------------------

    def validate_safety_bounds(self) -> None:
        """Enforce the bounds that configuration is not allowed to cross.

        Raises:
            PolicyConfigError: if the document would weaken a hard guarantee.
        """
        problems: list[str] = []

        ceilings = {"<default>": self.autonomy.default_ceiling_usd}
        ceilings.update(self.autonomy.category_ceilings_usd)
        for name, ceiling in ceilings.items():
            if ceiling < 0:
                problems.append(f"ceiling for {name} is negative ({ceiling})")
            if ceiling > ABSOLUTE_MAX_CEILING_USD:
                problems.append(
                    f"ceiling for {name} is {ceiling}, above the hard maximum "
                    f"{ABSOLUTE_MAX_CEILING_USD} compiled into the router"
                )

        bar = self.autonomy.confidence_threshold
        if not (MIN_CONFIDENCE_THRESHOLD <= bar <= Decimal("1")):
            problems.append(
                f"confidence_threshold {bar} outside the allowed range "
                f"[{MIN_CONFIDENCE_THRESHOLD}, 1]"
            )

        rate = self.fx_rates.get(self.base_currency.upper())
        if rate is not None and rate != Decimal("1"):
            problems.append(
                f"fx rate for the base currency {self.base_currency} must be 1, got {rate}"
            )
        for currency, value in self.fx_rates.items():
            if value <= 0:
                problems.append(f"fx rate for {currency} must be positive, got {value}")

        seen: set[str] = set()
        for rule in self.rules:
            if rule.rule_id in seen:
                problems.append(f"duplicate rule_id {rule.rule_id}")
            seen.add(rule.rule_id)

        if problems:
            raise PolicyConfigError("; ".join(problems))

    def enabled_rules(self, category: str) -> list[PolicyRule]:
        """Enabled rules that apply to ``category``, in document order."""
        return [r for r in self.rules if r.enabled and r.applies_to(category)]

    def enabled_rules_for(self, categories: list[str]) -> list[PolicyRule]:
        """Enabled rules applying to *any* of ``categories``, in document order.

        The router passes both the submitted and the agent-assigned category, so
        an item cannot escape a category cap by being re-labelled: the caps of
        every candidate category are evaluated. See
        :func:`services.router.app.decision.resolve_candidate_categories`.
        """
        wanted = [c.strip().lower() for c in categories if c and c.strip()] or ["other"]
        return [
            rule
            for rule in self.rules
            if rule.enabled and any(rule.applies_to(c) for c in wanted)
        ]

    def to_json_dict(self) -> dict[str, Any]:
        """JSON-safe dict (``Decimal`` rendered as string) for state/API use."""
        return json.loads(self.model_dump_json(by_alias=True))


def parse_policy_config(raw: dict[str, Any]) -> PolicyConfig:
    """Parse and safety-check a raw policy document.

    Raises:
        PolicyConfigError: on schema or safety-bound violations.
    """
    try:
        config = PolicyConfig.model_validate(raw)
    except ValidationError as exc:
        raise PolicyConfigError(f"invalid policy document: {exc}") from exc
    config.validate_safety_bounds()
    return config


def load_bootstrap_config(path: Path | str | None = None) -> PolicyConfig:
    """Load the bootstrap document from disk.

    Used on first start and by the unit tests, so the deterministic router can
    be exercised without a Dapr sidecar.
    """
    candidates = [Path(path)] if path else [_DEFAULT_CONFIG_PATH]
    if not path:
        # Repo-relative fallback so tests and local runs work outside Docker.
        candidates.append(
            Path(__file__).resolve().parents[3] / "config" / "policy-config.json"
        )
    for candidate in candidates:
        if candidate.is_file():
            with candidate.open(encoding="utf-8") as fh:
                return parse_policy_config(json.load(fh))
    raise PolicyConfigError(
        f"bootstrap policy config not found (looked in {[str(c) for c in candidates]})"
    )


_default_config: PolicyConfig | None = None


def default_policy_config() -> PolicyConfig:
    """Process-cached bootstrap configuration (synchronous, no Dapr needed)."""
    global _default_config
    if _default_config is None:
        _default_config = load_bootstrap_config()
    return _default_config


# ── Live store backed by Dapr state ─────────────────────────────────────────


class PolicyConfigStore:
    """Reads/writes the live policy configuration through the Dapr state store.

    A short TTL cache keeps the hot path cheap while still letting a change made
    by one replica reach every other replica without a restart.
    """

    def __init__(self, state_client: Any, ttl_seconds: float | None = None) -> None:
        self._state = state_client
        self._ttl = _TTL_SECONDS if ttl_seconds is None else ttl_seconds
        self._cached: PolicyConfig | None = None
        self._cached_at = 0.0

    def invalidate(self) -> None:
        self._cached = None
        self._cached_at = 0.0

    async def get(self) -> PolicyConfig:
        """Return the live configuration.

        Falls back to the bootstrap document (and seeds the state store with it)
        when nothing has been stored yet. Never returns a silently-empty policy.
        """
        now = time.monotonic()
        if self._cached is not None and (now - self._cached_at) < self._ttl:
            return self._cached

        raw = None
        try:
            raw = await self._state.get(POLICY_CONFIG_KEY)
        except Exception as exc:  # pragma: no cover - sidecar failure path
            logger.error("Policy config read from state store failed: %s", exc)

        if raw:
            try:
                config = parse_policy_config(raw)
            except PolicyConfigError as exc:
                # Never run on a document we cannot validate: fail loudly and
                # keep the last good copy (or the bootstrap one).
                logger.error("Stored policy config is invalid (%s), keeping last good copy", exc)
                config = self._cached or default_policy_config()
            self._cached, self._cached_at = config, now
            return config

        config = default_policy_config()
        logger.info(
            "No policy config in state store, seeding version %s from bootstrap file",
            config.version,
        )
        try:
            await self._state.save(POLICY_CONFIG_KEY, config.to_json_dict())
        except Exception as exc:  # pragma: no cover - sidecar failure path
            logger.error("Failed to seed policy config: %s", exc)
        self._cached, self._cached_at = config, now
        return config

    async def save(self, raw: dict[str, Any], updated_by: str) -> PolicyConfig:
        """Validate and persist a new configuration version.

        The version number is assigned by the service, not the caller, so the
        audit trail cannot be rewritten from outside.

        Raises:
            PolicyConfigError: if the new document is invalid or unsafe.
        """
        from datetime import datetime

        current = await self.get()
        candidate = dict(raw)
        candidate["version"] = current.version + 1
        candidate["updated_by"] = updated_by
        candidate["updated_at"] = datetime.now(UTC).isoformat()

        config = parse_policy_config(candidate)
        if not await self._state.save(POLICY_CONFIG_KEY, config.to_json_dict()):
            raise PolicyConfigError("failed to persist policy config to the state store")

        self._cached, self._cached_at = config, time.monotonic()
        logger.info(
            "Policy config updated to version %s by %s", config.version, updated_by
        )
        return config

    # -- policy prose (the document the agent reasons over) --------------

    async def get_document(self) -> tuple[str, int]:
        """Return ``(policy_markdown, version)`` for the RAG index.

        Bootstraps from the mounted ``policy.md`` the first time.
        """
        try:
            stored = await self._state.get(POLICY_DOCUMENT_KEY)
        except Exception as exc:  # pragma: no cover - sidecar failure path
            logger.error("Policy document read failed: %s", exc)
            stored = None

        if stored and stored.get("text"):
            return str(stored["text"]), int(stored.get("version", 1))

        text = _read_bootstrap_document()
        try:
            await self._state.save(POLICY_DOCUMENT_KEY, {"text": text, "version": 1})
        except Exception as exc:  # pragma: no cover - sidecar failure path
            logger.error("Failed to seed policy document: %s", exc)
        return text, 1

    async def save_document(self, text: str, updated_by: str) -> int:
        """Persist new policy prose and return its new version."""
        if not text.strip():
            raise PolicyConfigError("policy document must not be empty")
        _, version = await self.get_document()
        new_version = version + 1
        if not await self._state.save(
            POLICY_DOCUMENT_KEY,
            {"text": text, "version": new_version, "updated_by": updated_by},
        ):
            raise PolicyConfigError("failed to persist policy document")
        logger.info("Policy document updated to version %s by %s", new_version, updated_by)
        return new_version


def _read_bootstrap_document() -> str:
    for candidate in (
        _DEFAULT_DOCUMENT_PATH,
        Path(__file__).resolve().parents[3] / "policy.md",
    ):
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise PolicyConfigError(f"policy document not found at {_DEFAULT_DOCUMENT_PATH}")


Outcome = Literal["human_review", "reject", "duplicate"]
