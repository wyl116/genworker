# edition: baseline
"""Tests for DingTalk client."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.services.dingtalk import DingTalkClient, DingTalkConfig


@pytest.mark.asyncio
async def test_reply_message_uses_group_send_endpoint() -> None:
    http_client = AsyncMock()
    http_client.post = AsyncMock(return_value={"errcode": "0", "processQueryKey": "dk1"})
    client = DingTalkClient(
        DingTalkConfig(app_key="app", app_secret="secret", robot_code="robot"),
        v2_client=http_client,
        v1_client=AsyncMock(),
    )

    result = await client.reply_message("cid_1", "hello", token="tok")

    assert result["processQueryKey"] == "dk1"
    args = http_client.post.await_args
    assert args.args[0] == "/v1.0/robot/groupMessages/send"
    assert args.kwargs["json"]["conversationId"] == "cid_1"


@pytest.mark.asyncio
async def test_send_action_card_uses_action_card_msg_key() -> None:
    http_client = AsyncMock()
    http_client.post = AsyncMock(return_value={"errcode": "0", "processQueryKey": "dk2"})
    client = DingTalkClient(
        DingTalkConfig(app_key="app", app_secret="secret", robot_code="robot"),
        v2_client=http_client,
        v1_client=AsyncMock(),
    )

    await client.send_action_card("cid_2", {"title": "Card"}, token="tok")

    args = http_client.post.await_args
    assert args.kwargs["json"]["msgKey"] == "sampleActionCard"
