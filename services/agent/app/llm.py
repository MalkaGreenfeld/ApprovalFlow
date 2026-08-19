"""LLM providers, selected by configuration (M15).

A small registry keyed by LLM_PROVIDER:

* openai, groq, openrouter, together, local all speak the OpenAI wire format and
  differ only in base URL and default model;
* stub is a deterministic in-process provider, so CI and the eval harness never
  depend on a network call or a rate limit.

Credentials come from the Dapr secret store with an environment fallback. A
missing key for a real provider raises at startup rather than turning into a
confusing 401 on the first invoice.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

from approvalflow.logging import get_logger
from approvalflow.secrets import get_secret

logger = get_logger(__name__)


class LLMError(RuntimeError):
    """Raised when the provider cannot be used or the call fails."""


@dataclass(frozen=True)
class ProviderProfile:
    """Everything provider-specific in one place."""

    name: str
    base_url: str
    default_model: str
    requires_key: bool = True
    #: Some providers reject JSON-Schema keywords that OpenAI accepts. Gemini
    #: refuses ``additionalProperties`` and ``$schema`` even on its
    #: OpenAI-compatible endpoint, so the schema is sanitised before it is sent.
    sanitise_schema: bool = False


PROVIDERS: dict[str, ProviderProfile] = {
    "openai": ProviderProfile("openai", "https://api.openai.com/v1", "gpt-4o-mini"),
    "gemini": ProviderProfile(
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "gemini-2.0-flash",
        sanitise_schema=True,
    ),
    "groq": ProviderProfile(
        "groq", "https://api.groq.com/openai/v1", "llama-3.1-8b-instant"
    ),
    "openrouter": ProviderProfile(
        "openrouter", "https://openrouter.ai/api/v1", "google/gemini-flash-1.5"
    ),
    "together": ProviderProfile(
        "together", "https://api.together.xyz/v1",
        "meta-llama/Llama-3.1-8B-Instruct-Turbo",
    ),
    # Ollama / llama.cpp / vLLM served locally.
    "local": ProviderProfile(
        "local", "http://host.docker.internal:11434/v1", "llama3.1", requires_key=False
    ),
    "stub": ProviderProfile("stub", "", "stub-model", requires_key=False),
}


#: JSON-Schema keywords that some providers reject outright.
UNSUPPORTED_SCHEMA_KEYS = ("additionalProperties", "$schema", "definitions", "$defs")


def strip_unsupported_schema_keys(schema: Any) -> Any:
    """Recursively drop schema keywords a stricter provider will not accept.

    Gemini returns 400 for ``additionalProperties``, which OpenAI requires for a
    strict schema. Rather than keeping two copies of the schema, the one schema is
    filtered per provider on the way out.
    """
    if isinstance(schema, dict):
        return {
            k: strip_unsupported_schema_keys(v)
            for k, v in schema.items()
            if k not in UNSUPPORTED_SCHEMA_KEYS
        }
    if isinstance(schema, list):
        return [strip_unsupported_schema_keys(item) for item in schema]
    return schema


class ChatProvider(Protocol):
    """The one operation the analyser needs."""

    model: str

    async def complete_json(
        self, *, instructions: str, user_input: str, schema: dict[str, Any]
    ) -> dict[str, Any]: ...


class OpenAICompatibleProvider:
    """Any endpoint speaking the OpenAI API.

    Uses the Responses API with a strict JSON schema so the model's output is
    validated by the provider rather than parsed hopefully on our side.
    """

    def __init__(self, profile: ProviderProfile, api_key: str, model: str, timeout: float):
        from openai import AsyncOpenAI

        self.profile = profile
        self.model = model
        self._timeout = timeout
        self._client = AsyncOpenAI(
            api_key=api_key, base_url=profile.base_url, timeout=timeout, max_retries=2
        )

    async def complete_json(
        self, *, instructions: str, user_input: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        if self.profile.sanitise_schema:
            schema = strip_unsupported_schema_keys(schema)
        try:
            response = await self._client.responses.create(  # type: ignore[call-overload]
                model=self.model,
                instructions=instructions,
                input=user_input,
                temperature=float(os.getenv("LLM_TEMPERATURE", "0.1")),
                max_output_tokens=int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "1200")),
                text={"format": schema},
            )
        except Exception as exc:
            raise LLMError(f"{self.profile.name} call failed: {exc}") from exc

        content = getattr(response, "output_text", None) or ""
        if not content.strip():
            raise LLMError(f"{self.profile.name} returned an empty response")
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"{self.profile.name} returned non-JSON output") from exc


class StubProvider:
    """Deterministic provider for CI, the eval harness and offline demos.

    It is intentionally *not* a policy engine, it produces a plausible, honest
    recommendation from the obvious signals and lowers its confidence when it sees
    an attempt to steer the outcome. The router decides regardless, which is the
    whole point of the architecture: the tests still exercise every route with no
    model in the loop.
    """

    model = "stub-model"

    async def complete_json(
        self, *, instructions: str, user_input: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        fields = _parse_user_prompt(user_input)
        amount = _to_float(fields.get("Total (USD)") or fields.get("Total"))
        notes = (fields.get("Notes") or "").lower()
        category = (fields.get("Category (submitted)") or "other").lower()

        violations: list[dict[str, str]] = []
        confidence = 0.93
        recommendation = "auto_approve"

        if fields.get("Vendor Known", "").lower() in ("false", "no"):
            violations.append(
                {"rule_id": "GLOBAL-VENDOR", "description": "Vendor is not on file"}
            )
            recommendation, confidence = "human_review", 0.55
        if fields.get("Receipt Present", "").lower() in ("false", "no") and amount > 25:
            violations.append(
                {"rule_id": "GLOBAL-RECEIPT", "description": "Receipt missing"}
            )
            recommendation, confidence = "human_review", 0.5
        if fields.get("Math Reconciles", "").lower() in ("false", "no"):
            violations.append(
                {"rule_id": "GLOBAL-MATH", "description": "Line items do not sum to total"}
            )
            recommendation, confidence = "human_review", 0.4
        if "alcohol" in (fields.get("Line Items") or "").lower():
            violations.append(
                {"rule_id": "MEAL-03", "description": "Alcohol-only receipt"}
            )
            recommendation, confidence = "reject", 0.9
        if category == "other" and amount > 300:
            # Genuinely ambiguous bundles: say so instead of guessing.
            recommendation, confidence = "human_review", 0.6

        steering = ("approve me", "auto approve", "pay immediately", "no need to review",
                    "already ok", "finance already")
        if any(phrase in notes for phrase in steering):
            confidence = round(max(0.05, confidence - 0.2), 2)
            logger.warning("Stub provider detected outcome-steering language in notes")

        return {
            "recommendation": recommendation,
            "confidence": confidence,
            "violations": violations,
            "reasoning": (
                "Deterministic stub analysis based on vendor status, receipt, math "
                "reconciliation and category. No model was called."
            ),
            "amount_usd": amount,
            "category": category,
        }


def _parse_user_prompt(text: str) -> dict[str, str]:
    """Read back the ``Label: value`` lines the user prompt is built from."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            label, _, value = line.partition(":")
            out[label.strip()] = value.strip()
    return out


def _to_float(value: Any) -> float:
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


_provider: ChatProvider | None = None


def configured_provider_name() -> str:
    return os.getenv("LLM_PROVIDER", "openai").strip().lower()


async def get_provider() -> ChatProvider:
    """Build (once) the provider named by ``LLM_PROVIDER``.

    Raises:
        LLMError: for an unknown provider, or a missing key on a provider that
            needs one, never a silent fallback to a different model.
    """
    global _provider
    if _provider is not None:
        return _provider

    name = configured_provider_name()
    profile = PROVIDERS.get(name)
    if profile is None:
        raise LLMError(
            f"Unknown LLM_PROVIDER '{name}'. Supported: {', '.join(sorted(PROVIDERS))}"
        )

    if profile.name == "stub":
        logger.warning(
            "Using the stub LLM provider: deterministic, no model calls. "
            "Set LLM_PROVIDER to a real provider for the demo."
        )
        _provider = StubProvider()
        return _provider

    api_key = await get_secret(
        "llm:api_key", env_fallback="LLM_API_KEY", required=profile.requires_key
    )
    model = os.getenv("LLM_MODEL") or profile.default_model
    base_url = os.getenv("LLM_BASE_URL") or profile.base_url
    timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))

    logger.info(
        "LLM provider=%s model=%s base_url=%s", profile.name, model, base_url
    )
    _provider = OpenAICompatibleProvider(
        ProviderProfile(profile.name, base_url, model, profile.requires_key),
        api_key or "not-required",
        model,
        timeout,
    )
    return _provider


def reset_provider() -> None:
    """Drop the cached provider (tests, and after a credential rotation)."""
    global _provider
    _provider = None
