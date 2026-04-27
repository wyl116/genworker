# edition: baseline
from __future__ import annotations

from pathlib import Path

import pytest

from src.channels.models import ChannelInboundMessage, ReplyContent, build_channel_binding
from src.channels.registry import IMChannelRegistry
from src.channels.router import ChannelMessageRouter
from src.conversation.search_index import SessionSearchIndex
from src.conversation.session_manager import SessionManager
from src.conversation.session_store import FileSessionStore
from src.streaming.events import TextMessageEvent


class StubAdapter:
    def __init__(self) -> None:
        self.reply_calls: list[tuple[ChannelInboundMessage, ReplyContent]] = []

    def supports_streaming(self) -> bool:
        return False

    async def reply(self, source_msg: ChannelInboundMessage, content: ReplyContent) -> str:
        self.reply_calls.append((source_msg, content))
        return f"msg-{len(self.reply_calls)}"


class StubWorkerRouter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def route_stream(self, **kwargs):
        self.calls.append(kwargs)
        yield TextMessageEvent(
            run_id=f"run-{len(self.calls)}",
            content=f"handled: {kwargs['task']}",
        )


def _binding(channel_type: str, chat_id: str):
    return build_channel_binding(
        {
            "type": channel_type,
            "connection_mode": "webhook",
            "chat_ids": [chat_id],
            "reply_mode": "complete",
            "features": {},
        },
        tenant_id="demo",
        worker_id="analyst-01",
    )


@pytest.mark.asyncio
async def test_cross_channel_lookup_confirmation_reuses_other_channel_history(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    search_index = SessionSearchIndex(str(tmp_path / "session_search.sqlite"))
    await search_index.initialize()

    session_manager = SessionManager(
        FileSessionStore(workspace_root),
        search_index=search_index,
    )
    worker_router = StubWorkerRouter()
    registry = IMChannelRegistry()
    wecom_adapter = StubAdapter()
    feishu_adapter = StubAdapter()
    wecom_binding = _binding("wecom", "wx-chat")
    feishu_binding = _binding("feishu", "oc-chat")

    registry.register("wecom:demo:analyst-01", wecom_adapter, chat_ids=("wx-chat",))
    registry.register("feishu:demo:analyst-01", feishu_adapter, chat_ids=("oc-chat",))
    router = ChannelMessageRouter(
        session_manager=session_manager,
        worker_router=worker_router,
        registry=registry,
        bindings=(wecom_binding, feishu_binding),
        session_search_index=search_index,
    )

    await router.on_message(ChannelInboundMessage(
        message_id="wx-msg-1",
        channel_type="wecom",
        chat_id="wx-chat",
        chat_type="p2p",
        sender_id="alice",
        sender_name="Alice",
        content="Blue River approval needs finance signoff",
    ))

    await router.on_message(ChannelInboundMessage(
        message_id="fs-msg-1",
        channel_type="feishu",
        chat_id="oc-chat",
        chat_type="p2p",
        sender_id="alice",
        sender_name="Alice",
        content="继续 Blue River approval",
    ))

    assert len(worker_router.calls) == 1
    assert feishu_adapter.reply_calls[0][1].text.startswith("这条消息看起来像是在继续之前聊过的话题")

    feishu_thread_id = "im:feishu:oc-chat:alice"
    feishu_session = await session_manager.find_by_thread(feishu_thread_id)
    assert feishu_session is not None
    assert dict(feishu_session.metadata)["cross_channel_lookup_status"] == "awaiting_confirmation"

    await router.on_message(ChannelInboundMessage(
        message_id="fs-msg-2",
        channel_type="feishu",
        chat_id="oc-chat",
        chat_type="p2p",
        sender_id="alice",
        sender_name="Alice",
        content="是的，查一下之前记录",
    ))

    assert len(worker_router.calls) == 2
    assert worker_router.calls[1]["task"] == "Blue River approval"
    history_context = worker_router.calls[1]["task_context"]
    assert "[Potential Cross-Channel History]" in history_context
    assert "[Blue] [River] [approval]" in history_context
    assert "finance signoff" in history_context

    feishu_session = await session_manager.find_by_thread(feishu_thread_id)
    assert feishu_session is not None
    metadata = dict(feishu_session.metadata)
    assert "cross_channel_lookup_status" not in metadata
    assert "cross_channel_lookup_query" not in metadata

    router.close()
