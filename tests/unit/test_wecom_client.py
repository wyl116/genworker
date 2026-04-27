# edition: baseline
"""Tests for WeCom client."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.services.wecom import WeComClient, WeComConfig


@pytest.mark.asyncio
async def test_send_message_uses_query_param_auth():
    http_client = AsyncMock()
    http_client.post = AsyncMock(return_value={"errcode": 0, "msgid": "wx1"})
    client = WeComClient(
        WeComConfig(corpid="corp", corpsecret="secret", agent_id="1001"),
        http_client=http_client,
    )

    result = await client.send_message(("u1",), "hello", token="tok")

    assert result["msgid"] == "wx1"
    assert http_client.post.await_args.kwargs["auth_mode"] == "query_param"


@pytest.mark.asyncio
async def test_send_markdown_uses_markdown_payload() -> None:
    http_client = AsyncMock()
    http_client.post = AsyncMock(return_value={"errcode": 0, "msgid": "wx2"})
    client = WeComClient(
        WeComConfig(corpid="corp", corpsecret="secret", agent_id="1001"),
        http_client=http_client,
    )

    result = await client.send_markdown("chat-1", "# hello", token="tok")

    assert result["msgid"] == "wx2"
    assert http_client.post.await_args.kwargs["json"]["msgtype"] == "markdown"
    assert http_client.post.await_args.kwargs["json"]["chatid"] == "chat-1"
