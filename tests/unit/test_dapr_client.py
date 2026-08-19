"""Tests for DaprStateClient ETag-conditional save."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from approvalflow.dapr_client import DaprStateClient


def _mock_httpx_client(response: MagicMock) -> MagicMock:
    mock_client = AsyncMock()
    mock_client.get.return_value = response
    mock_client.post.return_value = response
    mock_ctx = MagicMock()
    mock_ctx.__aenter__.return_value = mock_client
    mock_ctx.__aexit__.return_value = None
    return mock_ctx


@pytest.mark.asyncio
async def test_get_with_etag_returns_value_and_etag():
    """get_with_etag should return both the state value and its ETag header."""
    response = MagicMock()
    response.status_code = 200
    response.text = '{"foo": "bar"}'
    response.json.return_value = {"foo": "bar"}
    response.headers = {"ETag": "etag-123"}

    state = DaprStateClient()
    with patch("httpx.AsyncClient", return_value=_mock_httpx_client(response)):
        value, etag = await state.get_with_etag("dedup:key")

    assert value == {"foo": "bar"}
    assert etag == "etag-123"


@pytest.mark.asyncio
async def test_get_with_etag_returns_none_when_key_missing():
    """A 204/empty response means no existing record — etag should be None too."""
    response = MagicMock()
    response.status_code = 204
    response.text = ""
    response.headers = {}

    state = DaprStateClient()
    with patch("httpx.AsyncClient", return_value=_mock_httpx_client(response)):
        value, etag = await state.get_with_etag("dedup:key")

    assert value is None
    assert etag is None


@pytest.mark.asyncio
async def test_save_with_etag_returns_true_on_success():
    """A conditional save with a matching ETag succeeds (200/201/204)."""
    response = MagicMock()
    response.status_code = 204

    state = DaprStateClient()
    with patch("httpx.AsyncClient", return_value=_mock_httpx_client(response)):
        result = await state.save_with_etag("dedup:key", {"foo": "bar"}, etag="etag-123")

    assert result is True


@pytest.mark.asyncio
async def test_save_with_etag_returns_false_on_conflict():
    """A stale ETag (concurrent writer won the race) must return False, not raise."""
    response = MagicMock()
    response.status_code = 409

    state = DaprStateClient()
    with patch("httpx.AsyncClient", return_value=_mock_httpx_client(response)):
        result = await state.save_with_etag("dedup:key", {"foo": "bar"}, etag="stale-etag")

    assert result is False
