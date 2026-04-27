# edition: baseline
"""Tests for Feishu client."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.services.feishu import FeishuClient, FeishuConfig


@pytest.mark.asyncio
async def test_list_with_metadata_returns_modified_time():
    http_client = AsyncMock()
    http_client.get = AsyncMock(return_value={
        "data": {
            "files": [
                {
                    "name": "spec.md",
                    "size": 12,
                    "type": "doc",
                    "modified_time": "2026-04-04T00:00:00Z",
                    "owner_id": "ou_1",
                }
            ]
        }
    })
    client = FeishuClient(
        FeishuConfig(app_id="id", app_secret="secret"),
        http_client=http_client,
    )

    files = await client.list_with_metadata({"folder_token": "fld"}, "/shared", "tok")

    assert files[0].name == "spec.md"
    assert files[0].modified_at == "2026-04-04T00:00:00Z"


@pytest.mark.asyncio
async def test_send_message_uses_im_api():
    http_client = AsyncMock()
    http_client.post = AsyncMock(return_value={"code": 0, "data": {"message_id": "m1"}})
    client = FeishuClient(
        FeishuConfig(app_id="id", app_secret="secret"),
        http_client=http_client,
    )

    await client.send_message(("ou_1",), "hello", token="tok")

    args = http_client.post.await_args
    assert args.args[0] == "/im/v1/messages"
    assert args.kwargs["token"] == "tok"
