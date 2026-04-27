# edition: baseline
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import subprocess
import sys
import time
from struct import pack
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.channels.adapters._sdk_runtime import build_reconnect_controller
from src.channels.commands import CommandDispatcher, CommandParser, CommandRegistry, CommandSpec
from src.channels.adapters.dingtalk_adapter import DingTalkIMAdapter
from src.channels.adapters.feishu_adapter import FeishuIMAdapter
from src.channels.adapters.wecom_adapter import WeComIMAdapter
from src.channels.models import (
    ChannelBinding,
    ChannelInboundMessage,
    Mention,
    ReplyContent,
    build_channel_binding,
    freeze_data,
)
from src.channels.registry import IMChannelRegistry
from src.channels.router import ChannelMessageRouter
from src.common.tenant import Tenant, TenantLoader, TrustLevel
from src.api.routes.channel_routes import router as channel_router
from src.conversation.models import ConversationSession
from src.conversation.search_index import SearchHit, SearchResult
from src.events.bus import EventBus
from src.events.models import Event
from src.streaming.events import TaskSpawnedEvent, TextMessageEvent
from src.channels.manager import _validate_chat_id_uniqueness


class StubSessionManager:
    def __init__(self) -> None:
        self.saved: ConversationSession | None = None
        self.last_thread_id = ""
        self._cache: dict[str, ConversationSession] = {}

    async def get_or_create(self, **kwargs):
        self.last_thread_id = kwargs["thread_id"]
        cached = self._cache.get(kwargs["thread_id"])
        if cached is not None:
            return cached
        session = ConversationSession(
            session_id="session-1",
            thread_id=kwargs["thread_id"],
            tenant_id=kwargs["tenant_id"],
            worker_id=kwargs["worker_id"],
            metadata=tuple(sorted((kwargs.get("metadata") or {}).items())),
        )
        self._cache[session.thread_id] = session
        return session

    async def save(self, session: ConversationSession) -> None:
        self.saved = session
        self._cache[session.thread_id] = session

    async def find_by_thread(self, thread_id: str):
        return self._cache.get(thread_id)


class StubWorkerRouter:
    def __init__(self) -> None:
        self.calls = []

    async def route_stream(self, **kwargs):
        self.calls.append(kwargs)
        yield TextMessageEvent(run_id="run-1", content="收到，开始处理。")
        yield TaskSpawnedEvent(
            run_id="run-1",
            task_id="task-1",
            task_description="生成周报",
        )

    def resolve_entry(self, *, task: str, tenant_id: str, worker_id: str):
        from src.worker.models import Worker, WorkerIdentity

        return SimpleNamespace(
            worker=Worker(
                identity=WorkerIdentity(name="Worker", worker_id=worker_id),
            )
        )


class StubSensorRegistry:
    def __init__(self) -> None:
        self.calls = []

    async def on_facts_sensed(self, facts, sensor_type):
        self.calls.append((facts, sensor_type))


class StubAdapter:
    def __init__(self) -> None:
        self.reply_calls = []
        self.send_calls = []

    def supports_streaming(self) -> bool:
        return False

    async def reply(self, source_msg, content):
        self.reply_calls.append((source_msg, content))
        return "msg-1"

    async def send_message(self, chat_id, content):
        self.send_calls.append((chat_id, content))
        return "msg-2"


class StubFeishuClient:
    def __init__(self) -> None:
        self.calls = []
        self._config = type("Cfg", (), {"app_id": "cli_a", "app_secret": "sec_b"})()

    async def reply_message(self, message_id, content, *, msg_type="text"):
        self.calls.append(("reply", message_id, content, msg_type))
        return {"data": {"message_id": "om_reply"}}

    async def send_card(self, chat_id, card):
        self.calls.append(("send_card", chat_id, card))
        return {"data": {"message_id": "om_card"}}

    async def update_card(self, message_id, card):
        self.calls.append(("update_card", message_id, card))
        return {"data": {"message_id": message_id}}

    async def send_chat_message(self, chat_id, content, *, msg_type="text"):
        self.calls.append(("send", chat_id, content, msg_type))
        return {"data": {"message_id": "om_send"}}


class StubWeComClient:
    def __init__(self) -> None:
        self.calls = []

    async def reply_message(self, chat_id, content, *, msg_type="text"):
        self.calls.append(("reply", chat_id, content, msg_type))
        return {"msgid": "wx_1", "errcode": 0}

    async def send_markdown(self, chat_id, content):
        self.calls.append(("markdown", chat_id, content))
        return {"msgid": "wx_md", "errcode": 0}


class StubDingTalkClient:
    def __init__(self) -> None:
        self.calls = []
        self._config = type("Cfg", (), {
            "app_key": "app_key",
            "app_secret": "app_secret",
            "robot_code": "robot_1",
        })()

    async def reply_message(self, conversation_id, content, *, msg_type="text"):
        self.calls.append(("reply", conversation_id, content, msg_type))
        return {"processQueryKey": "dk_1", "errcode": "0"}

    async def send_action_card(self, conversation_id, card):
        self.calls.append(("card", conversation_id, card))
        return {"processQueryKey": "dk_card", "errcode": "0"}

    async def send_interactive_card(self, conversation_id, card):
        self.calls.append(("interactive_card", conversation_id, card))
        return {"processQueryKey": "dk_card", "errcode": "0"}

    async def update_card(self, card_instance_id, card):
        self.calls.append(("update_card", card_instance_id, card))
        return {"processQueryKey": card_instance_id, "errcode": "0"}


class FakeRouteAdapter:
    async def handle_webhook(self, request):
        return {"status": "ok", "method": request.method}

    async def handle_interactivity(self, request):
        return {"status": "ok", "entry": "interactivity", "method": request.method}

    async def handle_slash_command(self, request):
        return {"status": "ok", "entry": "slash", "method": request.method}


class StubStreamingWorkerRouter:
    async def route_stream(self, **kwargs):
        yield TextMessageEvent(run_id="run-1", content="第一段")
        yield TextMessageEvent(run_id="run-1", content="第二段")
        yield TaskSpawnedEvent(
            run_id="run-1",
            task_id="task-2",
            task_description="后台汇总",
        )


class StubStreamingAdapter(StubAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.stream_chunks = []

    def supports_streaming(self) -> bool:
        return True

    async def reply_stream(self, source_msg, chunks):
        async for chunk in chunks:
            self.stream_chunks.append((chunk.chunk_type, chunk.content))
        return "stream-1"


class StubSessionSearchIndex:
    def __init__(self, hits: tuple[SearchHit, ...]) -> None:
        self.hits = hits
        self.calls: list[dict[str, object]] = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return SearchResult(
            hits=self.hits,
            total_count=len(self.hits),
            query=str(kwargs.get("query", "")),
        )


def _binding(**overrides) -> ChannelBinding:
    raw = {
        "type": "feishu",
        "connection_mode": "webhook",
        "chat_ids": ["oc_123"],
        "reply_mode": "complete",
        "features": {},
    }
    raw.update(overrides)
    return build_channel_binding(raw, tenant_id="demo", worker_id="analyst-01")


def _wecom_binding(**overrides) -> ChannelBinding:
    raw = {
        "type": "wecom",
        "connection_mode": "webhook",
        "chat_ids": ["chat_123"],
        "reply_mode": "complete",
        "features": {},
    }
    raw.update(overrides)
    return build_channel_binding(raw, tenant_id="demo", worker_id="analyst-01")


def _dingtalk_binding(**overrides) -> ChannelBinding:
    raw = {
        "type": "dingtalk",
        "connection_mode": "stream",
        "chat_ids": ["cid_123"],
        "reply_mode": "complete",
        "features": {},
    }
    raw.update(overrides)
    return build_channel_binding(raw, tenant_id="demo", worker_id="analyst-01")


def _email_binding(**overrides) -> ChannelBinding:
    raw = {
        "type": "email",
        "connection_mode": "poll",
        "chat_ids": ["support@corp.com"],
        "reply_mode": "complete",
        "features": {},
    }
    raw.update(overrides)
    return build_channel_binding(raw, tenant_id="demo", worker_id="analyst-01")


def _slack_binding(**overrides) -> ChannelBinding:
    raw = {
        "type": "slack",
        "connection_mode": "webhook",
        "chat_ids": ["*"],
        "reply_mode": "complete",
        "features": {},
    }
    raw.update(overrides)
    return build_channel_binding(raw, tenant_id="demo", worker_id="analyst-01")


@pytest.mark.asyncio
async def test_channel_message_router_replies_and_persists() -> None:
    session_manager = StubSessionManager()
    worker_router = StubWorkerRouter()
    registry = IMChannelRegistry()
    adapter = StubAdapter()
    registry.register("feishu:demo:analyst-01", adapter, chat_ids=("oc_123",))
    router = ChannelMessageRouter(
        session_manager=session_manager,
        worker_router=worker_router,
        registry=registry,
        bindings=(_binding(),),
    )

    await router.on_message(ChannelInboundMessage(
        message_id="om_1",
        channel_type="feishu",
        chat_id="oc_123",
        sender_id="ou_1",
        sender_name="Alice",
        content="帮我生成周报",
        mentions=(Mention(user_id="bot", name="bot", is_bot=True),),
    ))

    assert session_manager.last_thread_id == "im:feishu:oc_123:ou_1"
    assert session_manager.saved is not None
    assert session_manager.saved.spawned_tasks == ("task-1",)
    assert adapter.reply_calls[0][1].text == "收到，开始处理。\n\n已创建后台任务: 生成周报"
    assert worker_router.calls[0]["task_context"] == "channel_type:feishu"


def test_validate_chat_id_uniqueness_ignores_slack_wildcard_placeholder() -> None:
    other = build_channel_binding(
        {
            "type": "slack",
            "connection_mode": "webhook",
            "chat_ids": ["*"],
            "reply_mode": "complete",
            "features": {},
        },
        tenant_id="demo",
        worker_id="worker-2",
    )

    _validate_chat_id_uniqueness((_slack_binding(), other))


@pytest.mark.asyncio
async def test_email_router_uses_thread_root_and_enriches_session_metadata() -> None:
    session_manager = StubSessionManager()
    worker_router = StubWorkerRouter()
    registry = IMChannelRegistry()
    adapter = StubAdapter()
    registry.register("email:email_worker", adapter, chat_ids=("support@corp.com",))
    router = ChannelMessageRouter(
        session_manager=session_manager,
        worker_router=worker_router,
        registry=registry,
        bindings=(_email_binding(),),
    )

    await router.on_message(ChannelInboundMessage(
        message_id="msg-1",
        channel_type="email",
        chat_id="support@corp.com",
        chat_type="p2p",
        sender_id="alice@example.com",
        sender_name="Alice",
        content="[Need help]\nHello",
        reply_to_id="root-1",
        metadata=freeze_data({
            "subject": "Need help",
            "thread_root": "root-1",
            "in_reply_to": "parent-1",
        }),
    ))

    assert session_manager.last_thread_id == "im:email:support@corp.com:thread:root-1"
    assert session_manager.saved is not None
    assert dict(session_manager.saved.metadata)["subject"] == "Need help"
    assert worker_router.calls[0]["task_context"] == "channel_type:email, subject:Need help"


@pytest.mark.asyncio
async def test_channel_message_router_routes_group_message_to_sensor() -> None:
    sensor_registry = StubSensorRegistry()
    router = ChannelMessageRouter(
        session_manager=StubSessionManager(),
        worker_router=StubWorkerRouter(),
        registry=IMChannelRegistry(),
        bindings=(_binding(features={"monitor_group_chat": True}),),
        sensor_registries={"analyst-01": sensor_registry},
    )

    await router.dispatch(ChannelInboundMessage(
        message_id="om_2",
        channel_type="feishu",
        chat_id="oc_123",
        sender_id="ou_2",
        sender_name="Bob",
        content="群消息",
        mentions=(),
    ))

    assert len(sensor_registry.calls) == 1
    facts, sensor_type = sensor_registry.calls[0]
    assert sensor_type == "feishu"
    assert facts[0].event_type == "data.im.text"


@pytest.mark.asyncio
async def test_channel_message_router_streams_incremental_chunks() -> None:
    session_manager = StubSessionManager()
    registry = IMChannelRegistry()
    adapter = StubStreamingAdapter()
    registry.register("feishu:bot_main", adapter, chat_ids=("oc_123",))
    router = ChannelMessageRouter(
        session_manager=session_manager,
        worker_router=StubStreamingWorkerRouter(),
        registry=registry,
        bindings=(_binding(reply_mode="streaming"),),
    )

    await router.on_message(ChannelInboundMessage(
        message_id="om_stream_1",
        channel_type="feishu",
        chat_id="oc_123",
        sender_id="ou_stream",
        sender_name="Alice",
        content="开始流式",
        mentions=(Mention(user_id="bot", name="bot", is_bot=True),),
    ))

    assert adapter.stream_chunks == [
        ("text_delta", "第一段"),
        ("text_delta", "第二段"),
        ("text_delta", "\n\n已创建后台任务: 后台汇总"),
        ("finished", ""),
    ]
    assert session_manager.saved is not None
    assert session_manager.saved.messages[-1].content == "第一段\n\n第二段\n\n已创建后台任务: 后台汇总"


@pytest.mark.asyncio
async def test_channel_message_router_prompts_for_cross_channel_confirmation() -> None:
    session_manager = StubSessionManager()
    worker_router = StubWorkerRouter()
    registry = IMChannelRegistry()
    adapter = StubAdapter()
    search_index = StubSessionSearchIndex((
        SearchHit(
            session_id="session-old",
            thread_id="im:wecom:chat_9:user-1",
            role="user",
            content="之前聊过退款审批问题",
            snippet="之前聊过[退款审批]问题",
            created_at="2026-04-15T08:00:00+00:00",
            rank=0.1,
        ),
    ))
    registry.register("feishu:demo:analyst-01", adapter, chat_ids=("oc_123",))
    router = ChannelMessageRouter(
        session_manager=session_manager,
        worker_router=worker_router,
        registry=registry,
        bindings=(_binding(),),
        session_search_index=search_index,
    )

    await router.on_message(ChannelInboundMessage(
        message_id="om_ask_1",
        channel_type="feishu",
        chat_id="oc_123",
        sender_id="ou_1",
        sender_name="Alice",
        content="还是那个退款问题",
        mentions=(Mention(user_id="bot", name="bot", is_bot=True),),
    ))

    assert worker_router.calls == []
    assert adapter.reply_calls[0][1].text.startswith("这条消息看起来像是在继续之前聊过的话题")
    assert session_manager.saved is not None
    metadata = dict(session_manager.saved.metadata)
    assert metadata["cross_channel_lookup_status"] == "awaiting_confirmation"
    assert metadata["cross_channel_lookup_query"] == "退款问题"
    assert len(search_index.calls) == 1


@pytest.mark.asyncio
async def test_channel_message_router_skips_cross_channel_lookup_when_trust_disables_search(
    tmp_path,
) -> None:
    session_manager = StubSessionManager()
    worker_router = StubWorkerRouter()
    registry = IMChannelRegistry()
    adapter = StubAdapter()
    search_index = StubSessionSearchIndex((
        SearchHit(
            session_id="session-old",
            thread_id="im:wecom:chat_9:user-1",
            role="user",
            content="之前聊过退款审批问题",
            snippet="之前聊过[退款审批]问题",
            created_at="2026-04-15T08:00:00+00:00",
            rank=0.1,
        ),
    ))
    tenant_loader = TenantLoader(tmp_path)
    tenant_loader._cache["demo"] = Tenant(
        tenant_id="demo",
        name="Demo",
        trust_level=TrustLevel.BASIC,
    )
    registry.register("feishu:demo:analyst-01", adapter, chat_ids=("oc_123",))
    router = ChannelMessageRouter(
        session_manager=session_manager,
        worker_router=worker_router,
        registry=registry,
        bindings=(_binding(),),
        tenant_loader=tenant_loader,
        session_search_index=search_index,
    )

    await router.on_message(ChannelInboundMessage(
        message_id="om_ask_disabled_1",
        channel_type="feishu",
        chat_id="oc_123",
        sender_id="ou_1",
        sender_name="Alice",
        content="还是那个退款问题",
        mentions=(Mention(user_id="bot", name="bot", is_bot=True),),
    ))

    assert len(search_index.calls) == 0
    assert len(worker_router.calls) == 1
    assert worker_router.calls[0]["task"] == "还是那个退款问题"
    assert adapter.reply_calls[0][1].text.startswith("收到，开始处理。")


@pytest.mark.asyncio
async def test_channel_message_router_does_not_execute_command_above_trust_level(
    tmp_path,
) -> None:
    async def _restricted(ctx):
        return ReplyContent(text="restricted command executed")

    session_manager = StubSessionManager()
    worker_router = StubWorkerRouter()
    registry = IMChannelRegistry()
    adapter = StubAdapter()
    tenant_loader = TenantLoader(tmp_path)
    tenant_loader._cache["demo"] = Tenant(
        tenant_id="demo",
        name="Demo",
        trust_level=TrustLevel.BASIC,
    )
    command_registry = CommandRegistry()
    command_registry.register(
        CommandSpec(
            name="restricted",
            description="restricted",
            handler=_restricted,
            required_trust_level="FULL",
        )
    )
    registry.register("feishu:demo:analyst-01", adapter, chat_ids=("oc_123",))
    router = ChannelMessageRouter(
        session_manager=session_manager,
        worker_router=worker_router,
        registry=registry,
        bindings=(_binding(),),
        tenant_loader=tenant_loader,
        command_registry=command_registry,
        command_parser=CommandParser(command_registry),
        command_dispatcher=CommandDispatcher(),
    )

    await router.dispatch(ChannelInboundMessage(
        message_id="om_cmd_trust_1",
        channel_type="feishu",
        chat_id="oc_123",
        chat_type="p2p",
        sender_id="ou_1",
        sender_name="Alice",
        content="/restricted now",
        mentions=(Mention(user_id="bot", name="bot", is_bot=True),),
    ))

    assert worker_router.calls == []
    assert adapter.reply_calls[0][1].text == "命令 /restricted 当前租户权限不足，无法执行。"


@pytest.mark.asyncio
async def test_channel_message_router_does_not_fallback_to_worker_for_group_command_without_mention(
) -> None:
    async def _reset(ctx):
        return ReplyContent(text="reset")

    session_manager = StubSessionManager()
    worker_router = StubWorkerRouter()
    registry = IMChannelRegistry()
    adapter = StubAdapter()
    command_registry = CommandRegistry()
    command_registry.register(
        CommandSpec(
            name="reset",
            description="reset session",
            handler=_reset,
            require_mention=True,
        )
    )
    registry.register("feishu:demo:analyst-01", adapter, chat_ids=("oc_123",))
    router = ChannelMessageRouter(
        session_manager=session_manager,
        worker_router=worker_router,
        registry=registry,
        bindings=(_binding(),),
        tenant_loader=SimpleNamespace(
            load=lambda tenant_id: Tenant(
                tenant_id=tenant_id,
                name="Demo",
                trust_level=TrustLevel.STANDARD,
            )
        ),
        command_registry=command_registry,
        command_parser=CommandParser(command_registry),
        command_dispatcher=CommandDispatcher(),
    )

    await router.dispatch(ChannelInboundMessage(
        message_id="om_cmd_no_mention_1",
        channel_type="feishu",
        chat_id="oc_123",
        chat_type="group",
        sender_id="ou_1",
        sender_name="Alice",
        content="/reset",
        mentions=(),
    ))

    assert worker_router.calls == []
    assert adapter.reply_calls[0][1].text == "命令 /reset 需要在群聊中 @机器人 后执行。"


@pytest.mark.asyncio
async def test_channel_message_router_injects_updated_engine_dispatcher_into_command_context() -> None:
    captured = {}

    async def _resume(ctx):
        captured["engine_dispatcher"] = ctx.engine_dispatcher
        return ReplyContent(text="ok")

    session_manager = StubSessionManager()
    worker_router = StubWorkerRouter()
    registry = IMChannelRegistry()
    adapter = StubAdapter()
    command_registry = CommandRegistry()
    command_registry.register(
        CommandSpec(
            name="resume",
            description="resume flow",
            handler=_resume,
        )
    )
    registry.register("feishu:demo:analyst-01", adapter, chat_ids=("oc_123",))
    initial_dispatcher = object()
    router = ChannelMessageRouter(
        session_manager=session_manager,
        worker_router=worker_router,
        registry=registry,
        bindings=(_binding(),),
        tenant_loader=SimpleNamespace(
            load=lambda tenant_id: Tenant(
                tenant_id=tenant_id,
                name="Demo",
                trust_level=TrustLevel.STANDARD,
            )
        ),
        command_registry=command_registry,
        command_parser=CommandParser(command_registry),
        command_dispatcher=CommandDispatcher(),
        engine_dispatcher=initial_dispatcher,
    )
    updated_dispatcher = object()
    router.replace_runtime_dependencies(engine_dispatcher=updated_dispatcher)

    await router.dispatch(ChannelInboundMessage(
        message_id="om_cmd_engine_dispatcher_1",
        channel_type="feishu",
        chat_id="oc_123",
        chat_type="p2p",
        sender_id="ou_1",
        sender_name="Alice",
        content="/resume",
        mentions=(Mention(user_id="bot", name="bot", is_bot=True),),
    ))

    assert worker_router.calls == []
    assert captured["engine_dispatcher"] is updated_dispatcher
    assert adapter.reply_calls[0][1].text == "ok"


@pytest.mark.asyncio
async def test_channel_message_router_uses_history_after_confirmation() -> None:
    session_manager = StubSessionManager()
    worker_router = StubWorkerRouter()
    registry = IMChannelRegistry()
    adapter = StubAdapter()
    search_index = StubSessionSearchIndex((
        SearchHit(
            session_id="session-old",
            thread_id="im:wecom:chat_9:user-1",
            role="assistant",
            content="已和你在企微确认退款需要先补材料",
            snippet="已和你在企微确认[退款需要先补材料]",
            created_at="2026-04-15T08:00:00+00:00",
            rank=0.1,
        ),
    ))
    registry.register("feishu:demo:analyst-01", adapter, chat_ids=("oc_123",))
    router = ChannelMessageRouter(
        session_manager=session_manager,
        worker_router=worker_router,
        registry=registry,
        bindings=(_binding(),),
        session_search_index=search_index,
    )

    await router.on_message(ChannelInboundMessage(
        message_id="om_ask_2",
        channel_type="feishu",
        chat_id="oc_123",
        sender_id="ou_1",
        sender_name="Alice",
        content="还是那个退款问题",
        mentions=(Mention(user_id="bot", name="bot", is_bot=True),),
    ))

    await router.on_message(ChannelInboundMessage(
        message_id="om_confirm_1",
        channel_type="feishu",
        chat_id="oc_123",
        sender_id="ou_1",
        sender_name="Alice",
        content="是的，查一下之前记录",
        mentions=(Mention(user_id="bot", name="bot", is_bot=True),),
    ))

    assert len(worker_router.calls) == 1
    assert worker_router.calls[0]["task"] == "退款问题"
    assert "[Potential Cross-Channel History]" in worker_router.calls[0]["task_context"]
    assert "退款需要先补材料" in worker_router.calls[0]["task_context"]
    assert session_manager.saved is not None
    metadata = dict(session_manager.saved.metadata)
    assert "cross_channel_lookup_status" not in metadata
    assert len(search_index.calls) == 2


@pytest.mark.asyncio
async def test_channel_message_router_uses_configured_cross_channel_prompt() -> None:
    session_manager = StubSessionManager()
    worker_router = StubWorkerRouter()
    registry = IMChannelRegistry()
    adapter = StubAdapter()
    search_index = StubSessionSearchIndex((
        SearchHit(
            session_id="session-old",
            thread_id="im:wecom:chat_9:user-1",
            role="user",
            content="之前聊过报销单问题",
            snippet="之前聊过[报销单]问题",
            created_at="2026-04-15T08:00:00+00:00",
            rank=0.1,
        ),
    ))
    registry.register("feishu:demo:analyst-01", adapter, chat_ids=("oc_123",))
    router = ChannelMessageRouter(
        session_manager=session_manager,
        worker_router=worker_router,
        registry=registry,
        bindings=(_binding(features={
            "cross_channel_lookup_prompt": "这件事像是跨渠道延续，回复“是”后我再查历史。",
        }),),
        session_search_index=search_index,
    )

    await router.on_message(ChannelInboundMessage(
        message_id="om_ask_cfg_1",
        channel_type="feishu",
        chat_id="oc_123",
        sender_id="ou_1",
        sender_name="Alice",
        content="还是那个报销单问题",
        mentions=(Mention(user_id="bot", name="bot", is_bot=True),),
    ))

    assert adapter.reply_calls[0][1].text == "这件事像是跨渠道延续，回复“是”后我再查历史。"


@pytest.mark.asyncio
async def test_email_router_can_prompt_cross_channel_confirmation_by_subject() -> None:
    session_manager = StubSessionManager()
    worker_router = StubWorkerRouter()
    registry = IMChannelRegistry()
    adapter = StubAdapter()
    search_index = StubSessionSearchIndex((
        SearchHit(
            session_id="session-old",
            thread_id="im:feishu:oc_legacy:ou_legacy",
            role="assistant",
            content="之前在飞书确认项目代号 Blue River 需要先走审批",
            snippet="之前在飞书确认[Blue River]需要先走审批",
            created_at="2026-04-15T08:00:00+00:00",
            rank=0.1,
        ),
    ))
    registry.register("email:email_worker", adapter, chat_ids=("support@corp.com",))
    router = ChannelMessageRouter(
        session_manager=session_manager,
        worker_router=worker_router,
        registry=registry,
        bindings=(_email_binding(),),
        session_search_index=search_index,
    )

    await router.on_message(ChannelInboundMessage(
        message_id="msg-email-1",
        channel_type="email",
        chat_id="support@corp.com",
        chat_type="p2p",
        sender_id="alice@example.com",
        sender_name="Alice",
        content="[Blue River approval]\nPlease advise.",
        metadata=freeze_data({
            "subject": "Blue River approval",
        }),
    ))

    assert worker_router.calls == []
    assert adapter.reply_calls[0][1].text.startswith("这条消息看起来像是在继续之前聊过的话题")
    assert len(search_index.calls) == 1


@pytest.mark.asyncio
async def test_feishu_adapter_handles_challenge_and_message() -> None:
    client = StubFeishuClient()
    binding = _binding()
    adapter = FeishuIMAdapter(client, (binding,))
    captured = []

    async def _on_message(message):
        captured.append(message)

    await adapter.start(_on_message)

    app = FastAPI()
    app.include_router(channel_router)
    registry = IMChannelRegistry()
    registry.register("feishu:bot_main", adapter, chat_ids=("oc_123",))
    app.state.im_channel_registry = registry

    test_client = TestClient(app)
    challenge = test_client.post(
        "/api/v1/channel/feishu:bot_main/webhook",
        json={"type": "url_verification", "challenge": "abc"},
    )
    assert challenge.status_code == 200
    assert challenge.json() == {"challenge": "abc"}

    response = test_client.post(
        "/api/v1/channel/feishu:bot_main/webhook",
        json={
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "message": {
                    "message_id": "om_3",
                    "chat_id": "oc_123",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": "{\"text\":\"hello\"}",
                },
                "sender": {
                    "sender_id": {"open_id": "ou_3"},
                    "sender_type": "user",
                    "sender_name": "Carol",
                },
            },
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(captured) == 1
    assert captured[0].content == "hello"


def test_channel_routes_delegate_to_registered_adapter() -> None:
    app = FastAPI()
    app.include_router(channel_router)
    registry = IMChannelRegistry()
    registry.register("feishu:test", FakeRouteAdapter(), chat_ids=("oc_1",))
    app.state.im_channel_registry = registry

    client = TestClient(app)
    response = client.get("/api/v1/channel/feishu:test/webhook")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "method": "GET"}


def test_channel_routes_list_and_status() -> None:
    class HealthyAdapter(FakeRouteAdapter):
        async def health_check(self):
            return True

        def status_snapshot(self):
            return {"connection_state": "connected", "webhook_enabled": True}

    app = FastAPI()
    app.include_router(channel_router)
    registry = IMChannelRegistry()
    registry.register("feishu:test", HealthyAdapter(), chat_ids=("oc_1",))
    app.state.im_channel_registry = registry
    app.state.channel_manager = None

    client = TestClient(app)
    listed = client.get("/api/v1/channel")
    status = client.get("/api/v1/channel/feishu:test/status")

    assert listed.status_code == 200
    assert listed.json() == {"channels": ["feishu:test"]}
    assert status.status_code == 200
    assert status.json() == {
        "adapter_id": "feishu:test",
        "healthy": True,
        "details": {"connection_state": "connected", "webhook_enabled": True},
    }


def test_channel_routes_delegate_to_slack_interactivity_and_slash_handlers() -> None:
    app = FastAPI()
    app.include_router(channel_router)
    registry = IMChannelRegistry()
    registry.register("slack:test", FakeRouteAdapter(), chat_ids=())
    app.state.im_channel_registry = registry

    client = TestClient(app)
    interactivity = client.post(
        "/api/v1/channel/slack:test/interactivity",
        content="payload={}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    slash = client.post(
        "/api/v1/channel/slack:test/slash",
        content="command=%2Fhelp",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert interactivity.status_code == 200
    assert interactivity.json() == {
        "status": "ok",
        "entry": "interactivity",
        "method": "POST",
    }
    assert slash.status_code == 200
    assert slash.json() == {
        "status": "ok",
        "entry": "slash",
        "method": "POST",
    }


def test_reconnect_controller_exponential_backoff_and_circuit_breaker() -> None:
    controller = build_reconnect_controller({
        "reconnect_backoff_base_seconds": 0.01,
        "reconnect_backoff_max_seconds": 0.04,
        "reconnect_max_retries": 5,
        "reconnect_jitter_ratio": 0.0,
        "circuit_failure_threshold": 3,
        "circuit_recovery_timeout_seconds": 0.05,
    })

    delay1 = controller.register_failure(RuntimeError("first"))
    delay2 = controller.register_failure(RuntimeError("second"))
    delay3 = controller.register_failure(RuntimeError("third"))
    snapshot = controller.snapshot()

    assert delay1 == pytest.approx(0.01)
    assert delay2 == pytest.approx(0.02)
    assert delay3 == pytest.approx(0.05)
    assert snapshot["breaker_state"] == "open"
    assert snapshot["consecutive_failures"] == 3
    assert snapshot["current_backoff_seconds"] == pytest.approx(0.05)
    assert snapshot["circuit_open_until"]


def test_reconnect_controller_applies_jitter(monkeypatch) -> None:
    monkeypatch.setattr("src.channels.adapters._sdk_runtime.random.uniform", lambda low, high: 1.1)
    controller = build_reconnect_controller({
        "reconnect_backoff_base_seconds": 0.1,
        "reconnect_backoff_max_seconds": 1.0,
        "reconnect_jitter_ratio": 0.2,
        "reconnect_max_retries": 5,
        "circuit_failure_threshold": 5,
        "circuit_recovery_timeout_seconds": 1.0,
    })

    delay = controller.register_failure(RuntimeError("first"))
    snapshot = controller.snapshot()

    assert delay == pytest.approx(0.11)
    assert snapshot["current_backoff_seconds"] == pytest.approx(0.11)


@pytest.mark.asyncio
async def test_wecom_adapter_handles_get_and_post_webhook() -> None:
    client = StubWeComClient()
    binding = _wecom_binding(features={"callback_token": "token123"})
    adapter = WeComIMAdapter(client, (binding,))
    captured = []

    async def _on_message(message):
        captured.append(message)

    await adapter.start(_on_message)

    app = FastAPI()
    app.include_router(channel_router)
    registry = IMChannelRegistry()
    registry.register("wecom:corp_bot", adapter, chat_ids=("chat_123",))
    app.state.im_channel_registry = registry
    test_client = TestClient(app)

    challenge = test_client.get(
        "/api/v1/channel/wecom:corp_bot/webhook",
        params={"echostr": "hello"},
    )
    assert challenge.status_code == 200
    assert challenge.json() == {"echostr": "hello"}

    payload = (
        "<xml><MsgId>m1</MsgId><FromUserName>user1</FromUserName>"
        "<ConversationId>chat_123</ConversationId><MsgType>text</MsgType>"
        "<Content>hello</Content></xml>"
    )
    response = test_client.post(
        "/api/v1/channel/wecom:corp_bot/webhook",
        content=payload,
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(captured) == 1
    assert captured[0].content == "hello"


@pytest.mark.asyncio
async def test_wecom_adapter_reply_stream_sends_progressive_markdown() -> None:
    client = StubWeComClient()
    adapter = WeComIMAdapter(
        client,
        (_wecom_binding(reply_mode="streaming"),),
    )

    async def _chunks():
        yield SimpleNamespace(chunk_type="text_delta", content="第一段")
        yield SimpleNamespace(chunk_type="text_delta", content="第二段")
        yield SimpleNamespace(chunk_type="finished", content="")

    message = ChannelInboundMessage(
        message_id="wx_stream_1",
        channel_type="wecom",
        chat_id="chat_123",
    )

    await adapter.reply_stream(message, _chunks())

    assert client.calls[0] == ("markdown", "chat_123", "第一段")
    assert client.calls[-1] == ("markdown", "chat_123", "第一段第二段")


@pytest.mark.asyncio
async def test_wecom_adapter_decrypts_encrypted_challenge_and_message() -> None:
    client = StubWeComClient()
    aes_key = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
    binding = _wecom_binding(features={
        "callback_token": "token123",
        "encoding_aes_key": aes_key,
    })
    adapter = WeComIMAdapter(client, (binding,))
    adapter._client._config = type("Cfg", (), {"corpid": "corp-1"})()
    captured = []

    async def _on_message(message):
        captured.append(message)

    await adapter.start(_on_message)

    app = FastAPI()
    app.include_router(channel_router)
    registry = IMChannelRegistry()
    registry.register("wecom:corp_bot", adapter, chat_ids=("chat_123",))
    app.state.im_channel_registry = registry
    test_client = TestClient(app)

    encrypted_echo = _encrypt_wecom_payload("hello", aes_key, "corp-1")
    echo_sig = _wecom_signature("token123", "1", "2", encrypted_echo)
    challenge = test_client.get(
        "/api/v1/channel/wecom:corp_bot/webhook",
        params={
            "echostr": encrypted_echo,
            "msg_signature": echo_sig,
            "timestamp": "1",
            "nonce": "2",
        },
    )
    assert challenge.status_code == 200
    assert challenge.json() == {"echostr": "hello"}

    inner_xml = (
        "<xml><MsgId>m2</MsgId><FromUserName>user2</FromUserName>"
        "<ConversationId>chat_123</ConversationId><MsgType>text</MsgType>"
        "<Content>secure hello</Content></xml>"
    )
    encrypted_xml = _encrypt_wecom_payload(inner_xml, aes_key, "corp-1")
    payload = f"<xml><Encrypt><![CDATA[{encrypted_xml}]]></Encrypt></xml>"
    msg_sig = _wecom_signature("token123", "3", "4", encrypted_xml)
    response = test_client.post(
        "/api/v1/channel/wecom:corp_bot/webhook",
        params={
            "msg_signature": msg_sig,
            "timestamp": "3",
            "nonce": "4",
        },
        content=payload,
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(captured) == 1
    assert captured[0].content == "secure hello"


@pytest.mark.asyncio
async def test_dingtalk_adapter_handles_challenge_and_message() -> None:
    client = StubDingTalkClient()
    adapter = DingTalkIMAdapter(client, (_dingtalk_binding(),))
    captured = []

    async def _on_message(message):
        captured.append(message)

    await adapter.start(_on_message)

    app = FastAPI()
    app.include_router(channel_router)
    registry = IMChannelRegistry()
    registry.register("dingtalk:robot_main", adapter, chat_ids=("cid_123",))
    app.state.im_channel_registry = registry
    test_client = TestClient(app)

    challenge = test_client.post(
        "/api/v1/channel/dingtalk:robot_main/webhook",
        json={"challenge": "xyz"},
    )
    assert challenge.status_code == 200
    assert challenge.json() == {"challenge": "xyz"}

    response = test_client.post(
        "/api/v1/channel/dingtalk:robot_main/webhook",
        json={
            "conversationId": "cid_123",
            "senderId": "user_1",
            "senderNick": "Dora",
            "text": {"content": "hi"},
            "msgtype": "text",
            "msgId": "msg_1",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(captured) == 1
    assert captured[0].sender_name == "Dora"


@pytest.mark.asyncio
async def test_dingtalk_adapter_reply_stream_updates_card() -> None:
    client = StubDingTalkClient()
    adapter = DingTalkIMAdapter(client, (_dingtalk_binding(reply_mode="streaming"),))

    async def _chunks():
        yield SimpleNamespace(chunk_type="text_delta", content="第一段")
        yield SimpleNamespace(chunk_type="text_delta", content="第二段")
        yield SimpleNamespace(chunk_type="finished", content="")

    message = ChannelInboundMessage(
        message_id="dk_stream_1",
        channel_type="dingtalk",
        chat_id="cid_123",
    )

    result = await adapter.reply_stream(message, _chunks())

    assert result == "dk_card"
    assert client.calls[0][0] == "interactive_card"
    assert client.calls[1][0] == "update_card"
    assert client.calls[-1] == (
        "update_card",
        "dk_card",
        {"title": "助手回复", "markdown": "第一段第二段", "status": "finished"},
    )


@pytest.mark.asyncio
async def test_feishu_adapter_starts_optional_websocket_sdk(monkeypatch) -> None:
    fake_module = _build_fake_feishu_sdk_module()
    monkeypatch.setitem(sys.modules, "lark_oapi", fake_module)

    client = StubFeishuClient()
    adapter = FeishuIMAdapter(client, (_binding(connection_mode="websocket"),))
    captured = []

    async def _on_message(message):
        captured.append(message)

    await adapter.start(_on_message)
    await _wait_for(lambda: len(captured) == 1)

    assert await adapter.health_check() is True
    assert captured[0].content == "来自长连接"
    await adapter.stop()
    assert fake_module._last_ws_client.stopped is True


@pytest.mark.asyncio
async def test_feishu_adapter_websocket_falls_back_when_sdk_missing(monkeypatch) -> None:
    monkeypatch.setattr("src.channels.adapters.feishu_adapter.optional_import", lambda _: None)

    client = StubFeishuClient()
    adapter = FeishuIMAdapter(client, (_binding(connection_mode="websocket"),))

    async def _ignore(_message):
        return None

    await adapter.start(_ignore)

    assert await adapter.health_check() is False
    await adapter.stop()


@pytest.mark.asyncio
async def test_dingtalk_adapter_starts_optional_stream_sdk(monkeypatch) -> None:
    fake_module = _build_fake_dingtalk_sdk_module()
    monkeypatch.setitem(sys.modules, "dingtalk_stream", fake_module)

    client = StubDingTalkClient()
    adapter = DingTalkIMAdapter(client, (_dingtalk_binding(connection_mode="stream"),))
    captured = []

    async def _on_message(message):
        captured.append(message)

    await adapter.start(_on_message)
    await _wait_for(lambda: len(captured) == 1)

    assert await adapter.health_check() is True
    assert captured[0].content == "stream hello"
    await adapter.stop()
    assert fake_module._last_stream_client.stopped is True


@pytest.mark.asyncio
async def test_dingtalk_adapter_stream_falls_back_when_sdk_missing(monkeypatch) -> None:
    monkeypatch.setattr("src.channels.adapters.dingtalk_adapter.optional_import", lambda _: None)

    client = StubDingTalkClient()
    adapter = DingTalkIMAdapter(client, (_dingtalk_binding(connection_mode="stream"),))

    async def _ignore(_message):
        return None

    await adapter.start(_ignore)

    assert await adapter.health_check() is False
    await adapter.stop()


@pytest.mark.asyncio
async def test_feishu_adapter_reconnects_after_transient_websocket_failure(monkeypatch) -> None:
    fake_module = _build_fake_feishu_sdk_module(fail_first=1)
    monkeypatch.setitem(sys.modules, "lark_oapi", fake_module)

    client = StubFeishuClient()
    adapter = FeishuIMAdapter(client, (_binding(
        connection_mode="websocket",
        features={
            "reconnect_backoff_base_seconds": 0.01,
            "reconnect_backoff_max_seconds": 0.02,
            "reconnect_jitter_ratio": 0.0,
            "circuit_failure_threshold": 3,
            "circuit_recovery_timeout_seconds": 0.03,
        },
    ),))
    captured = []

    async def _on_message(message):
        captured.append(message)

    await adapter.start(_on_message)
    await _wait_for(lambda: len(captured) == 1, timeout=2.0)

    snapshot = adapter.status_snapshot()
    assert snapshot["healthy"] is True
    assert snapshot["reconnect_attempts"] >= 1
    assert snapshot["breaker_state"] == "closed"
    assert snapshot["last_connected_at"]
    await adapter.stop()


@pytest.mark.asyncio
async def test_dingtalk_adapter_reconnects_after_transient_stream_failure(monkeypatch) -> None:
    fake_module = _build_fake_dingtalk_sdk_module(fail_first=1)
    monkeypatch.setitem(sys.modules, "dingtalk_stream", fake_module)

    client = StubDingTalkClient()
    adapter = DingTalkIMAdapter(client, (_dingtalk_binding(
        connection_mode="stream",
        features={
            "reconnect_backoff_base_seconds": 0.01,
            "reconnect_backoff_max_seconds": 0.02,
            "reconnect_jitter_ratio": 0.0,
            "circuit_failure_threshold": 3,
            "circuit_recovery_timeout_seconds": 0.03,
        },
    ),))
    captured = []

    async def _on_message(message):
        captured.append(message)

    await adapter.start(_on_message)
    await _wait_for(lambda: len(captured) == 1, timeout=2.0)

    snapshot = adapter.status_snapshot()
    assert snapshot["healthy"] is True
    assert snapshot["reconnect_attempts"] >= 1
    assert snapshot["breaker_state"] == "closed"
    assert snapshot["last_connected_at"]
    await adapter.stop()


@pytest.mark.asyncio
async def test_feishu_adapter_opens_circuit_after_repeated_failures(monkeypatch) -> None:
    fake_module = _build_fake_feishu_sdk_module(always_fail=True)
    monkeypatch.setitem(sys.modules, "lark_oapi", fake_module)

    client = StubFeishuClient()
    adapter = FeishuIMAdapter(client, (_binding(
        connection_mode="websocket",
        features={
            "reconnect_backoff_base_seconds": 0.01,
            "reconnect_backoff_max_seconds": 0.02,
            "reconnect_jitter_ratio": 0.0,
            "circuit_failure_threshold": 3,
            "circuit_recovery_timeout_seconds": 0.05,
        },
    ),))

    async def _ignore(_message):
        return None

    await adapter.start(_ignore)
    try:
        await _wait_for(lambda: adapter.status_snapshot()["breaker_state"] == "open", timeout=1.0)

        snapshot = adapter.status_snapshot()
        assert snapshot["connection_state"] in {"degraded", "reconnecting"}
        assert snapshot["breaker_state"] == "open"
        assert snapshot["current_backoff_seconds"] == pytest.approx(0.05)
        assert snapshot["circuit_open_until"]
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_channel_router_sends_task_completed_notification() -> None:
    event_bus = EventBus()
    registry = IMChannelRegistry()
    adapter = StubAdapter()
    registry.register("feishu:bot_main", adapter, chat_ids=("oc_123",))
    router = ChannelMessageRouter(
        session_manager=StubSessionManager(),
        worker_router=StubWorkerRouter(),
        registry=registry,
        bindings=(_binding(),),
        event_bus=event_bus,
    )

    await event_bus.publish(Event(
        event_id="evt-1",
        type="task.completed",
        source="test",
        tenant_id="demo",
        payload=(
            ("task_id", "task-1"),
            ("description", "生成周报"),
            ("thread_id", "im:feishu:oc_123:ou_1"),
        ),
    ))

    assert adapter.send_calls
    assert adapter.send_calls[0][0] == "oc_123"
    assert adapter.send_calls[0][1].text == "任务已完成: 生成周报"
    router.close()


@pytest.mark.asyncio
async def test_channel_router_sends_task_failed_notification() -> None:
    event_bus = EventBus()
    registry = IMChannelRegistry()
    adapter = StubAdapter()
    registry.register("feishu:bot_main", adapter, chat_ids=("oc_123",))
    router = ChannelMessageRouter(
        session_manager=StubSessionManager(),
        worker_router=StubWorkerRouter(),
        registry=registry,
        bindings=(_binding(),),
        event_bus=event_bus,
    )

    await event_bus.publish(Event(
        event_id="evt-failed-1",
        type="task.failed",
        source="test",
        tenant_id="demo",
        payload=(
            ("task_id", "task-1"),
            ("thread_id", "im:feishu:oc_123:ou_1"),
            ("error_message", "quota exhausted"),
        ),
    ))

    assert adapter.send_calls
    assert adapter.send_calls[0][0] == "oc_123"
    assert adapter.send_calls[0][1].text == "任务执行失败: task-1\n原因: quota exhausted"
    router.close()


@pytest.mark.asyncio
async def test_email_task_completed_notification_injects_recipient_metadata() -> None:
    event_bus = EventBus()
    session_manager = StubSessionManager()
    session_manager._cache["im:email:support@corp.com:thread:root-1"] = ConversationSession(
        session_id="session-email",
        thread_id="im:email:support@corp.com:thread:root-1",
        tenant_id="demo",
        worker_id="analyst-01",
        metadata=(
            ("sender_id", "alice@example.com"),
            ("subject", "Need help"),
        ),
    )
    registry = IMChannelRegistry()
    adapter = StubAdapter()
    registry.register("email:email_worker", adapter, chat_ids=("support@corp.com",))
    router = ChannelMessageRouter(
        session_manager=session_manager,
        worker_router=StubWorkerRouter(),
        registry=registry,
        bindings=(_email_binding(),),
        event_bus=event_bus,
    )

    await event_bus.publish(Event(
        event_id="evt-2",
        type="task.completed",
        source="test",
        tenant_id="demo",
        payload=(
            ("task_id", "task-2"),
            ("description", "处理完成"),
            ("thread_id", "im:email:support@corp.com:thread:root-1"),
        ),
    ))

    assert adapter.send_calls
    assert dict(adapter.send_calls[0][1].metadata) == {
        "recipient": "alice@example.com",
        "subject": "Need help",
    }
    router.close()


@pytest.mark.asyncio
async def test_email_task_failed_notification_injects_recipient_metadata() -> None:
    event_bus = EventBus()
    session_manager = StubSessionManager()
    session_manager._cache["im:email:support@corp.com:thread:root-1"] = ConversationSession(
        session_id="session-email",
        thread_id="im:email:support@corp.com:thread:root-1",
        tenant_id="demo",
        worker_id="analyst-01",
        metadata=(
            ("sender_id", "alice@example.com"),
            ("subject", "Need help"),
        ),
    )
    registry = IMChannelRegistry()
    adapter = StubAdapter()
    registry.register("email:email_worker", adapter, chat_ids=("support@corp.com",))
    router = ChannelMessageRouter(
        session_manager=session_manager,
        worker_router=StubWorkerRouter(),
        registry=registry,
        bindings=(_email_binding(),),
        event_bus=event_bus,
    )

    await event_bus.publish(Event(
        event_id="evt-failed-2",
        type="task.failed",
        source="test",
        tenant_id="demo",
        payload=(
            ("task_id", "task-2"),
            ("thread_id", "im:email:support@corp.com:thread:root-1"),
            ("error_message", "timeout"),
        ),
    ))

    assert adapter.send_calls
    assert adapter.send_calls[0][1].text == "任务执行失败: task-2\n原因: timeout"
    assert dict(adapter.send_calls[0][1].metadata) == {
        "recipient": "alice@example.com",
        "subject": "Need help",
    }
    router.close()


def _encrypt_wecom_payload(message: str, encoding_aes_key: str, corp_id: str) -> str:
    aes_key = base64.b64decode(f"{encoding_aes_key}=")
    iv = aes_key[:16]
    random_bytes = os.urandom(16)
    raw = random_bytes + pack(">I", len(message.encode("utf-8"))) + message.encode("utf-8") + corp_id.encode("utf-8")
    pad = 32 - (len(raw) % 32)
    raw += bytes([pad]) * pad
    completed = subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-256-cbc",
            "-K",
            aes_key.hex(),
            "-iv",
            iv.hex(),
            "-nopad",
        ],
        input=raw,
        capture_output=True,
        check=True,
    )
    return base64.b64encode(completed.stdout).decode("utf-8")


def _wecom_signature(token: str, timestamp: str, nonce: str, value: str) -> str:
    return hashlib.sha1("".join(sorted([token, timestamp, nonce, value])).encode("utf-8")).hexdigest()


async def _wait_for(check, timeout: float = 0.5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not satisfied before timeout")


def _build_fake_feishu_sdk_module(*, fail_first: int = 0, always_fail: bool = False) -> ModuleType:
    module = ModuleType("lark_oapi")
    module._start_failures_left = fail_first
    module._always_fail = always_fail

    class _DispatcherBuilder:
        def __init__(self) -> None:
            self.callback = None

        def register_p2_im_message_receive_v1(self, callback):
            self.callback = callback
            return self

        def build(self):
            return self.callback

    class _EventDispatcherHandler:
        @staticmethod
        def builder(*args):
            return _DispatcherBuilder()

    class _OpenAPIBuilder:
        def __init__(self) -> None:
            self.app_id_value = ""
            self.app_secret_value = ""

        def app_id(self, value):
            self.app_id_value = value
            return self

        def app_secret(self, value):
            self.app_secret_value = value
            return self

        def build(self):
            return {
                "app_id": self.app_id_value,
                "app_secret": self.app_secret_value,
            }

    class _OpenAPIClient:
        @staticmethod
        def builder():
            return _OpenAPIBuilder()

    class _WSClient:
        def __init__(self, api_client, event_handler=None, **kwargs):
            self.api_client = api_client
            self.handler = event_handler or kwargs.get("handler") or kwargs.get("dispatcher")
            self.started = False
            self.stopped = False
            module._last_ws_client = self

        def start(self):
            self.started = True
            if module._always_fail:
                raise RuntimeError("persistent websocket failure")
            if module._start_failures_left > 0:
                module._start_failures_left -= 1
                raise RuntimeError("transient websocket failure")
            if callable(self.handler):
                self.handler({
                    "message": {
                        "message_id": "om_ws_1",
                        "chat_id": "oc_123",
                        "chat_type": "group",
                        "message_type": "text",
                        "content": "{\"text\":\"来自长连接\"}",
                    },
                    "sender": {
                        "sender_id": {"open_id": "ou_ws_1"},
                        "sender_type": "user",
                        "sender_name": "LongConn",
                    },
                })
            while not self.stopped:
                time.sleep(0.01)

        def stop(self):
            self.stopped = True

        def is_running(self):
            return self.started and not self.stopped

    module.EventDispatcherHandler = _EventDispatcherHandler
    module.Client = _OpenAPIClient
    module.ws = SimpleNamespace(Client=_WSClient)
    module._last_ws_client = None
    return module


def _build_fake_dingtalk_sdk_module(*, fail_first: int = 0, always_fail: bool = False) -> ModuleType:
    module = ModuleType("dingtalk_stream")
    module._start_failures_left = fail_first
    module._always_fail = always_fail

    class _Credential:
        def __init__(self, app_key, app_secret):
            self.app_key = app_key
            self.app_secret = app_secret

    class _AckMessage:
        STATUS_OK = "OK"

    class _ChatbotMessage:
        TOPIC = "chatbot.message"

    class _ChatbotHandler:
        pass

    class _StreamClient:
        def __init__(self, credential):
            self.credential = credential
            self.handler = None
            self.started = False
            self.stopped = False
            module._last_stream_client = self

        def register_callback_handler(self, topic, handler):
            self.topic = topic
            self.handler = handler

        async def start(self):
            self.started = True
            if module._always_fail:
                raise RuntimeError("persistent stream failure")
            if module._start_failures_left > 0:
                module._start_failures_left -= 1
                raise RuntimeError("transient stream failure")
            if self.handler is not None:
                await self.handler.process(SimpleNamespace(data={
                    "conversationId": "cid_123",
                    "senderId": "user_stream_1",
                    "senderNick": "StreamUser",
                    "text": {"content": "stream hello"},
                    "msgtype": "text",
                    "msgId": "stream_msg_1",
                }))
            while not self.stopped:
                await asyncio.sleep(0.01)

        def stop(self):
            self.stopped = True

        def is_running(self):
            return self.started and not self.stopped

    module.Credential = _Credential
    module.AckMessage = _AckMessage
    module.ChatbotMessage = _ChatbotMessage
    module.ChatbotHandler = _ChatbotHandler
    module.DingTalkStreamClient = _StreamClient
    module._last_stream_client = None
    return module
