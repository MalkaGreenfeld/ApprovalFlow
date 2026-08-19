"""Dapr secret-store client (M5).

The secret store is the primary source for the LLM key and the JWT signing key.
An environment variable is kept as a fallback, logged when used, for local runs
without a sidecar.

Values are cached for the life of the process: secrets are read on the cold path
(startup, first LLM call), never per request.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .logging import get_logger

logger = get_logger(__name__)

#: Must match the component name in dapr/components/secrets.yaml.
DAPR_SECRET_STORE = os.getenv("DAPR_SECRET_STORE", "secrets-store")
#: The local-file secret store uses ':' as the nesting separator, so the key
#: "llm:api_key" resolves to secrets.json -> {"llm": {"api_key": ...}}.
NESTED_SEPARATOR = ":"

_cache: dict[str, str] = {}


class SecretNotFoundError(RuntimeError):
    """Raised when a required secret exists in neither Dapr nor the environment."""


async def get_secret(
    name: str,
    *,
    env_fallback: str | None = None,
    default: str | None = None,
    required: bool = False,
) -> str | None:
    """Fetch a secret, preferring the Dapr secret store.

    Args:
        name: Dapr secret name, e.g. ``"llm:api_key"``.
        env_fallback: Environment variable consulted when Dapr has no value.
        default: Returned when neither source has a value.
        required: Raise instead of returning ``default``.

    Raises:
        SecretNotFoundError: when ``required`` and nothing was found.
    """
    if name in _cache:
        return _cache[name]

    value = await _read_from_dapr(name)
    source = "dapr"

    if value is None and env_fallback:
        value = os.getenv(env_fallback)
        source = f"env:{env_fallback}"
        if value:
            logger.warning(
                "Secret '%s' came from %s, not the Dapr secret store", name, source
            )

    if value is None:
        if required:
            raise SecretNotFoundError(
                f"required secret '{name}' not found in the Dapr secret store "
                f"'{DAPR_SECRET_STORE}' or environment variable '{env_fallback}'"
            )
        return default

    _cache[name] = value
    logger.info("Loaded secret '%s' from %s", name, source)
    return value


async def _read_from_dapr(name: str) -> str | None:
    port = os.getenv("DAPR_HTTP_PORT", "3500")
    url = f"http://localhost:{port}/v1.0/secrets/{DAPR_SECRET_STORE}/{name}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5)) as client:
            resp = await client.get(url)
    except Exception as exc:
        logger.warning("Dapr secret store unreachable for '%s': %s", name, exc)
        return None

    if resp.status_code != 200:
        logger.warning(
            "Dapr secret '%s' unavailable (HTTP %s)", name, resp.status_code
        )
        return None

    payload: dict[str, Any] = resp.json()
    # Dapr returns {"<name>": "<value>"} for the local file store.
    value = payload.get(name)
    if value is None and len(payload) == 1:
        value = next(iter(payload.values()))
    return str(value) if value is not None else None


def clear_cache() -> None:
    """Drop cached secrets (used by tests and after a credential rotation)."""
    _cache.clear()
