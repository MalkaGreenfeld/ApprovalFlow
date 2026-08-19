"""Dapr HTTP API clients, state management and service invocation.

Shared across all ApprovalFlow services. Wraps the Dapr sidecar HTTP API
(localhost:{DAPR_HTTP_PORT}/v1.0/...) using httpx for async HTTP.

Usage::

    from approvalflow.dapr_client import DaprStateClient

    state = DaprStateClient()
    await state.save("submission:abc-123", {"status": "received", ...})
    record = await state.get("submission:abc-123")
    results = await state.query({"filter": {"vendor": "Acme Corp"}})
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

import httpx

from .logging import get_logger

logger = get_logger(__name__)

DAPR_HTTP_PORT = os.getenv("DAPR_HTTP_PORT", "3500")
DAPR_STATE_STORE = "statestore"
DAPR_BASE = f"http://localhost:{DAPR_HTTP_PORT}/v1.0"


class ConcurrentUpdateError(RuntimeError):
    """Raised when an optimistic-concurrency loop keeps losing the race."""


class DaprStateClient:
    """Async client for Dapr state management API.

    Talks to the Dapr sidecar at ``localhost:{DAPR_HTTP_PORT}``.
    All methods are async and use an internal ``httpx.AsyncClient``.
    """

    def __init__(self, store_name: str = DAPR_STATE_STORE) -> None:
        self._store = store_name
        self._base = DAPR_BASE

    # ------------------------------------------------------------------
    # Key/value CRUD
    # ------------------------------------------------------------------

    async def save(
        self, key: str, value: dict[str, Any], etag: str | None = None
    ) -> bool:
        """Save a state object, optionally with optimistic concurrency.

        Args:
            key: State key (e.g. ``"submission:uuid"``).
            value: JSON-serialisable dictionary to persist.
            etag: When supplied, the write only succeeds if the stored version
                still carries this ETag. A concurrent writer makes the store
                answer 409 and this method return ``False``.

        Returns:
            ``True`` if the save succeeded.
        """
        url = f"{self._base}/state/{self._store}"
        entry: dict[str, Any] = {"key": key, "value": value}
        if etag is not None:
            entry["etag"] = etag
            # first-write-wins: reject rather than clobber a concurrent update
            entry["options"] = {"concurrency": "first-write", "consistency": "strong"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as client:
            resp = await client.post(url, json=[entry])
            if resp.status_code == 409:
                logger.info("ETag conflict saving %s, caller should retry", key)
                return False
            return resp.status_code in (200, 201, 204)

    async def save_with_etag(
        self, key: str, value: dict[str, Any], etag: str | None
    ) -> bool:
        """Conditional save, spelled out as its own method.

        Equivalent to ``save(key, value, etag=etag)``. Kept because a
        check-then-act caller reads better when the conditional write is named:
        the caller treats ``False`` as "someone else got there first", not as an
        error.
        """
        return await self.save(key, value, etag=etag)

    async def get_with_etag(self, key: str) -> tuple[dict[str, Any] | None, str | None]:
        """Retrieve a value together with its ETag.

        Returns:
            ``(value, etag)``; ``(None, None)`` when the key does not exist.
        """
        url = f"{self._base}/state/{self._store}/{key}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as client:
            resp = await client.get(url, params={"consistency": "strong"})
            if resp.status_code != 200 or resp.text.strip() == "":
                return None, None
            return resp.json(), resp.headers.get("ETag")

    async def update_atomic(
        self,
        key: str,
        mutate: Callable[[dict[str, Any] | None], dict[str, Any]],
        *,
        retries: int = 5,
    ) -> dict[str, Any]:
        """Compare-and-set loop for moving a shared counter.

        Used for department budgets: a write that lost the ETag check replays
        ``mutate`` against the freshly read value, so two concurrent approvals
        cannot both spend the same money.

        Args:
            key: State key holding the counter.
            mutate: Function ``current -> new``, re-invoked on every retry. It may
                raise to abort the update.
            retries: Attempts before giving up.

        Raises:
            ConcurrentUpdateError: if every attempt lost the race.
        """
        for attempt in range(1, retries + 1):
            current, etag = await self.get_with_etag(key)
            new_value = mutate(current)
            if await self.save(key, new_value, etag=etag):
                return new_value
            logger.warning(
                "Optimistic concurrency retry %s/%s for key %s", attempt, retries, key
            )
            await asyncio.sleep(0.05 * attempt)
        raise ConcurrentUpdateError(
            f"could not update {key} after {retries} attempts, too much contention"
        )

    async def get(self, key: str) -> dict[str, Any] | None:
        """Retrieve a state object by key.

        Args:
            key: State key.

        Returns:
            The value dictionary, or ``None`` if not found.
        """
        url = f"{self._base}/state/{self._store}/{key}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as client:
            resp = await client.get(url)
            if resp.status_code == 204 or resp.text.strip() == "":
                return None
            if resp.status_code == 200:
                return resp.json()
            return None

    async def delete(self, key: str) -> bool:
        """Delete a state object.

        Args:
            key: State key.

        Returns:
            ``True`` if deleted (HTTP 200/204).
        """
        url = f"{self._base}/state/{self._store}/{key}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as client:
            resp = await client.delete(url)
            return resp.status_code in (200, 204)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def query(self, filter_dict: dict[str, Any]) -> list[dict[str, Any]]:
        """Query the state store with a JSON filter.

        Requires the state store component to have ``indexed: true``.

        Args:
            filter_dict: Dapr query filter, e.g.
                ``{"filter": {"EQ": {"vendor": "Acme Corp"}}}``.

        Returns:
            List of matching state objects (each ``{key, value, ...}``).
        """
        url = f"{self._base}/state/{self._store}/query"
        async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as client:
            resp = await client.post(url, json=filter_dict)
            if resp.status_code in (200, 204):
                data = resp.json()
                results = data.get("results", [])
                return results
            return []

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    async def get_state(self, key: str) -> dict[str, Any] | None:
        """Alias for :meth:`get`."""
        return await self.get(key)

    async def save_state(self, key: str, value: dict[str, Any]) -> bool:
        """Alias for :meth:`save`."""
        return await self.save(key, value)


#: Where each app listens, used only as the fallback described in
#: :class:`DaprInvokeClient`. Overridable so the ports are not baked in.
DIRECT_PORTS: dict[str, str] = {
    "ingestion": os.getenv("INGESTION_PORT", "8001"),
    "agent": os.getenv("AGENT_PORT", "8002"),
    "router": os.getenv("ROUTER_PORT", "8003"),
    "payment": os.getenv("PAYMENT_PORT", "8004"),
    "notification": os.getenv("NOTIFICATION_PORT", "8005"),
}


class DaprInvokeClient:
    """Async client for Dapr service-to-service invocation (M5).

    Dapr invocation is the primary path: the caller names an *app-id*, and the
    sidecar handles discovery, retries and mTLS. Usage::

        invoke = DaprInvokeClient()
        resp = await invoke.call("router", "internal/decisions/abc", http_verb="GET")

    Why there is a fallback
    -----------------------
    In self-hosted mode Dapr discovers apps over **mDNS**, and an mDNS entry can
    go stale for a while after a container restarts, the caller's sidecar keeps
    dialling an address that has moved and the call fails even though the target
    is healthy. That is survivable for a fire-and-forget message, but the audit
    trail (F9) is assembled from these calls, and "the decision history is
    temporarily missing" is not an acceptable answer to an auditor.

    So a failed invocation falls back to the target's own DNS name on the compose
    network, which Docker resolves reliably. The fallback is logged, so using it
    is visible rather than silent, and it is only ever a second attempt: normal
    operation still goes through Dapr.
    """

    def __init__(self, allow_direct_fallback: bool = True) -> None:
        self._base = DAPR_BASE
        self._allow_direct = allow_direct_fallback

    async def call(
        self,
        app_id: str,
        method: str,
        data: dict[str, Any] | None = None,
        http_verb: str = "POST",
    ) -> dict[str, Any] | None:
        """Invoke a method on another Dapr app.

        Args:
            app_id: Dapr app-id of the target service (e.g. ``"ingestion"``).
            method: Method path on the target (e.g. ``"api/submissions"``).
            data: Optional JSON body.
            http_verb: HTTP verb (default ``"POST"``).

        Returns:
            The JSON response, or ``None`` when both the Dapr call and the direct
            fallback fail. Callers treat ``None`` as "source unavailable" and say
            so in their response rather than pretending the data does not exist.
        """
        result = await self._request(
            f"{self._base}/invoke/{app_id}/method/{method}", data, http_verb
        )
        if result is not None or not self._allow_direct:
            return result

        port = DIRECT_PORTS.get(app_id)
        if port is None:
            return None
        logger.warning(
            "Dapr invocation of %s/%s failed, retrying directly over the service "
            "network (sidecar discovery may be stale after a restart)",
            app_id, method,
        )
        return await self._request(f"http://{app_id}:{port}/{method}", data, http_verb)

    async def _request(
        self, url: str, data: dict[str, Any] | None, http_verb: str
    ) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as client:
                if http_verb.upper() == "GET":
                    resp = await client.get(url)
                else:
                    resp = await client.post(url, json=data)
                if resp.status_code in (200, 201, 204):
                    try:
                        return resp.json()
                    except ValueError:
                        return None
                return None
        except httpx.HTTPError as exc:
            logger.warning("Call to %s failed: %s", url, exc)
            return None
