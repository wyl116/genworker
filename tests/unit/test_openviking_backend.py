# edition: baseline
from __future__ import annotations

import json

import httpx
import pytest

from src.memory.backends.openviking import OpenVikingClient


def _transport():
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        payload = json.loads(request.content.decode("utf-8")) if request.content else {}
        if request.url.path == "/v1/index":
            return httpx.Response(200, json={"id": payload.get("id") or "mem-1"})
        if request.url.path == "/v1/search":
            return httpx.Response(200, json={
                "results": [
                    {
                        "id": "mem-1",
                        "abstract": "remembered fact",
                        "metadata": {"tenant_id": "demo", "ts": "2026-04-16T00:00:00+00:00"},
                        "score": 0.91,
                    }
                ]
            })
        return httpx.Response(200, json={})

    return httpx.MockTransport(_handler)


@pytest.mark.asyncio
async def test_openviking_client_index_and_search():
    client = OpenVikingClient(
        endpoint="http://openviking.test",
        http_client=httpx.AsyncClient(
            base_url="http://openviking.test",
            transport=_transport(),
        ),
    )

    item_id = await client.index(
        scope="viking://tenant/demo/worker/worker-1/memories/semantic",
        content="remembered fact",
        metadata={"tenant_id": "demo"},
    )
    result = await client.search(
        scope="viking://tenant/demo/worker/worker-1/memories/semantic",
        query="remembered",
    )

    assert item_id == "mem-1"
    assert len(result) == 1
    assert result[0].display_text == "remembered fact"


@pytest.mark.asyncio
async def test_openviking_client_health_check():
    client = OpenVikingClient(
        endpoint="http://openviking.test",
        http_client=httpx.AsyncClient(
            base_url="http://openviking.test",
            transport=_transport(),
        ),
    )

    assert await client.health_check() is True
