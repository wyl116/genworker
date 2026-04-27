# edition: baseline
"""Tests for shared HTTP client helpers."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.services._http_base import BaseAPIClient, BaseAPIConfig, BaseAPIError


def _response(
    *,
    status_code: int = 200,
    json_body: dict | None = None,
    content: bytes = b"",
    headers: dict[str, str] | None = None,
):
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {"content-type": "application/json"}
    response.json.return_value = json_body or {}
    response.content = content
    response.text = content.decode("utf-8", errors="ignore")
    response.raise_for_status.return_value = None
    return response


@pytest.mark.asyncio
async def test_bearer_auth_header_injected():
    client = BaseAPIClient(BaseAPIConfig(base_url="https://example.com"))
    http_client = AsyncMock()
    http_client.request.return_value = _response(json_body={"ok": True})
    with patch.object(client, "_get_client", AsyncMock(return_value=http_client)):
        await client.get("/ping", token="tok-1")
    headers = http_client.request.await_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok-1"


@pytest.mark.asyncio
async def test_query_param_auth_injected():
    client = BaseAPIClient(BaseAPIConfig(base_url="https://example.com"))
    http_client = AsyncMock()
    http_client.request.return_value = _response(json_body={"ok": True})
    with patch.object(client, "_get_client", AsyncMock(return_value=http_client)):
        await client.get("/ping", token="tok-1", auth_mode="query_param")
    params = http_client.request.await_args.kwargs["params"]
    assert params["access_token"] == "tok-1"


@pytest.mark.asyncio
async def test_http_status_error_raises_base_error():
    client = BaseAPIClient(BaseAPIConfig(base_url="https://example.com", max_retries=1))
    response = _response(status_code=400, json_body={"code": 400})
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "bad request",
        request=MagicMock(),
        response=response,
    )
    http_client = AsyncMock()
    http_client.request.return_value = response
    with patch.object(client, "_get_client", AsyncMock(return_value=http_client)):
        with pytest.raises(BaseAPIError):
            await client.get("/ping", token="tok-1")
