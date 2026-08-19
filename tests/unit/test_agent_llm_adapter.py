"""Provider selection and per-provider schema translation (M15).

Carried over from the provider-adapter work on dev and repointed at the merged
provider registry in ``services/agent/app/llm.py``. The behaviours asserted are
the same: the default provider, selecting Gemini, caching, refusing an unknown
name, and stripping schema keywords a stricter provider rejects.
"""

from __future__ import annotations

import pytest

from services.agent.app.llm import (
    PROVIDERS,
    LLMError,
    OpenAICompatibleProvider,
    StubProvider,
    configured_provider_name,
    get_provider,
    reset_provider,
    strip_unsupported_schema_keys,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    reset_provider()
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    yield
    reset_provider()


@pytest.fixture
def openai_installed():
    """Building a real provider imports the openai client library.

    CI installs it from the agent's requirements; a machine that only has the
    shared package skips these three rather than failing.
    """
    return pytest.importorskip("openai")


# ── Provider selection ──────────────────────────────────────────────────────


def test_the_default_provider_is_openai(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert configured_provider_name() == "openai"


@pytest.mark.asyncio
async def test_gemini_is_selectable_and_carries_its_own_base_url(
    monkeypatch, openai_installed
):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    provider = await get_provider()

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.profile.name == "gemini"
    assert provider.model == PROVIDERS["gemini"].default_model
    assert "generativelanguage.googleapis.com" in PROVIDERS["gemini"].base_url


@pytest.mark.asyncio
async def test_the_model_can_be_overridden_by_configuration(monkeypatch, openai_installed):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("LLM_MODEL", "gemini-2.5-pro")

    provider = await get_provider()

    assert provider.model == "gemini-2.5-pro"


@pytest.mark.asyncio
async def test_the_provider_is_built_once_and_cached(monkeypatch, openai_installed):
    monkeypatch.setenv("LLM_PROVIDER", "openai")

    first = await get_provider()
    second = await get_provider()

    assert first is second


@pytest.mark.asyncio
async def test_an_unknown_provider_raises_instead_of_falling_back(monkeypatch):
    """A typo must not silently route to a different model."""
    monkeypatch.setenv("LLM_PROVIDER", "not-a-real-provider")

    with pytest.raises(LLMError, match="Unknown LLM_PROVIDER"):
        await get_provider()


@pytest.mark.asyncio
async def test_the_stub_provider_needs_no_credentials(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    assert isinstance(await get_provider(), StubProvider)


def test_every_registered_provider_has_a_base_url_and_default_model():
    for name, profile in PROVIDERS.items():
        assert profile.default_model, f"{name} has no default model"
        if name != "stub":
            assert profile.base_url, f"{name} has no base URL"


# ── Schema translation ──────────────────────────────────────────────────────


def test_unsupported_schema_keys_are_stripped_recursively():
    """Gemini rejects additionalProperties, which OpenAI requires for strictness."""
    schema = {
        "type": "json_schema",
        "name": "agent_output",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "violations": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": False},
                }
            },
        },
    }

    cleaned = strip_unsupported_schema_keys(schema)

    assert "$schema" not in cleaned
    assert "additionalProperties" not in cleaned["schema"]
    assert "additionalProperties" not in cleaned["schema"]["properties"]["violations"]["items"]
    # The parts that carry meaning survive.
    assert cleaned["name"] == "agent_output"
    assert cleaned["schema"]["properties"]["violations"]["type"] == "array"


def test_stripping_leaves_a_clean_schema_untouched():
    schema = {"type": "object", "properties": {"confidence": {"type": "number"}}}
    assert strip_unsupported_schema_keys(schema) == schema


def test_only_gemini_is_marked_as_needing_sanitising():
    assert PROVIDERS["gemini"].sanitise_schema is True
    assert PROVIDERS["openai"].sanitise_schema is False
