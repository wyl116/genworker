# edition: baseline
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autonomy.inbox import InboxItem, SessionInboxStore
from src.channels.commands.approval_events import register_approval_event_type
from src.channels.commands.builtin import build_builtin_command_registry
from src.channels.commands.models import CommandContext
from src.channels.models import ChannelInboundMessage, build_channel_binding
from src.common.tenant import Tenant, TrustLevel


class _StubSessionManager:
    async def find_by_thread(self, thread_id: str):
        return SimpleNamespace(session_id="session-1")


class _StubScheduler:
    def __init__(self) -> None:
        self.payloads = []

    async def submit_langgraph_resume(self, payload: dict, *, priority: int) -> bool:
        self.payloads.append((payload, priority))
        return True


class _ExplodingScheduler:
    async def submit_langgraph_resume(self, payload: dict, *, priority: int) -> bool:
        raise RuntimeError("scheduler offline")


def _ctx(tmp_path: Path, inbox_store, scheduler):
    binding = build_channel_binding(
        {"type": "feishu", "connection_mode": "webhook", "chat_ids": ["oc_1"]},
        tenant_id="tenant-1",
        worker_id="worker-1",
    )
    return CommandContext(
        message=ChannelInboundMessage(
            message_id="m1",
            channel_type="feishu",
            chat_id="oc_1",
            sender_id="user-1",
            content="",
        ),
        binding=binding,
        tenant=Tenant(tenant_id="tenant-1", name="Tenant", trust_level=TrustLevel.STANDARD),
        args={"argv": ("inbox-1", "同意")},
        session_manager=_StubSessionManager(),
        thread_id="thread-1",
        inbox_store=inbox_store,
        worker_schedulers={"worker-1": scheduler},
        task_store=SimpleNamespace(save=lambda *_args, **_kwargs: None),
    )


@pytest.mark.asyncio
async def test_approve_confirmation_routes_langgraph_resume(tmp_path: Path):
    store = SessionInboxStore(fallback_dir=tmp_path)
    await store.write(
        InboxItem(
            inbox_id="inbox-1",
            tenant_id="tenant-1",
            worker_id="worker-1",
            source_type="langgraph",
            event_type="langgraph.interrupt",
            payload={
                "engine": "langgraph",
                "thread_id": "run-1",
                "skill_id": "approval-flow",
                "state_digest": "digest-1",
            },
        )
    )
    scheduler = _StubScheduler()
    ctx = _ctx(tmp_path, store, scheduler)

    reply = await build_builtin_command_registry().resolve("approve_confirmation").handler(ctx)

    assert "流程恢复中" in reply.text
    assert scheduler.payloads
    payload, priority = scheduler.payloads[0]
    assert payload["thread_id"] == "run-1"
    assert payload["skill_id"] == "approval-flow"
    assert payload["decision"]["approved"] is True
    assert priority == 15


@pytest.mark.asyncio
async def test_approve_confirmation_langgraph_does_not_require_task_store(tmp_path: Path):
    register_approval_event_type("order_approval")
    store = SessionInboxStore(fallback_dir=tmp_path)
    await store.write(
        InboxItem(
            inbox_id="inbox-1",
            tenant_id="tenant-1",
            worker_id="worker-1",
            source_type="langgraph",
            event_type="order_approval",
            payload={
                "engine": "langgraph",
                "thread_id": "run-1",
                "skill_id": "approval-flow",
                "state_digest": "digest-1",
            },
        )
    )
    scheduler = _StubScheduler()
    ctx = _ctx(tmp_path, store, scheduler)
    ctx = CommandContext(
        **{
            **ctx.__dict__,
            "task_store": None,
        }
    )

    reply = await build_builtin_command_registry().resolve("approve_confirmation").handler(ctx)

    assert "流程恢复中" in reply.text
    assert scheduler.payloads


@pytest.mark.asyncio
async def test_approve_confirmation_langgraph_scheduler_exception_requeues(tmp_path: Path):
    store = SessionInboxStore(fallback_dir=tmp_path)
    await store.write(
        InboxItem(
            inbox_id="inbox-1",
            tenant_id="tenant-1",
            worker_id="worker-1",
            source_type="langgraph",
            event_type="langgraph.interrupt",
            payload={
                "engine": "langgraph",
                "thread_id": "run-1",
                "skill_id": "approval-flow",
                "state_digest": "digest-1",
            },
        )
    )
    ctx = _ctx(tmp_path, store, _ExplodingScheduler())

    reply = await build_builtin_command_registry().resolve("approve_confirmation").handler(ctx)

    assert "流程提交调度失败" in reply.text
    assert "scheduler offline" in reply.text
    stored = await store.get_by_id("inbox-1", tenant_id="tenant-1", worker_id="worker-1")
    assert stored is not None
    assert stored.status == "PENDING"


@pytest.mark.asyncio
async def test_reject_confirmation_langgraph_scheduler_exception_requeues(tmp_path: Path):
    store = SessionInboxStore(fallback_dir=tmp_path)
    await store.write(
        InboxItem(
            inbox_id="inbox-1",
            tenant_id="tenant-1",
            worker_id="worker-1",
            source_type="langgraph",
            event_type="langgraph.interrupt",
            payload={
                "engine": "langgraph",
                "thread_id": "run-1",
                "skill_id": "approval-flow",
                "state_digest": "digest-1",
            },
        )
    )
    ctx = CommandContext(
        **{
            **_ctx(tmp_path, store, _ExplodingScheduler()).__dict__,
            "args": {"argv": ("inbox-1", "拒绝原因")},
        }
    )

    reply = await build_builtin_command_registry().resolve("reject_confirmation").handler(ctx)

    assert "流程提交调度失败" in reply.text
    assert "scheduler offline" in reply.text
    stored = await store.get_by_id("inbox-1", tenant_id="tenant-1", worker_id="worker-1")
    assert stored is not None
    assert stored.status == "PENDING"


@pytest.mark.asyncio
async def test_reject_confirmation_routes_langgraph_resume(tmp_path: Path):
    store = SessionInboxStore(fallback_dir=tmp_path)
    await store.write(
        InboxItem(
            inbox_id="inbox-1",
            tenant_id="tenant-1",
            worker_id="worker-1",
            source_type="langgraph",
            event_type="langgraph.interrupt",
            payload={
                "engine": "langgraph",
                "thread_id": "run-1",
                "skill_id": "approval-flow",
                "state_digest": "digest-1",
            },
        )
    )
    scheduler = _StubScheduler()
    ctx = CommandContext(
        **{
            **_ctx(tmp_path, store, scheduler).__dict__,
            "args": {"argv": ("inbox-1", "不同意")},
        }
    )

    reply = await build_builtin_command_registry().resolve("reject_confirmation").handler(ctx)

    assert "流程恢复中" in reply.text
    assert scheduler.payloads
    payload, priority = scheduler.payloads[0]
    assert payload["thread_id"] == "run-1"
    assert payload["skill_id"] == "approval-flow"
    assert payload["decision"]["approved"] is False
    assert payload["decision"]["note"] == "不同意"
    assert priority == 15


@pytest.mark.asyncio
async def test_reject_confirmation_langgraph_accepts_custom_event_type_without_task_store(tmp_path: Path):
    register_approval_event_type("order_approval_reject")
    store = SessionInboxStore(fallback_dir=tmp_path)
    await store.write(
        InboxItem(
            inbox_id="inbox-1",
            tenant_id="tenant-1",
            worker_id="worker-1",
            source_type="langgraph",
            event_type="order_approval_reject",
            payload={
                "engine": "langgraph",
                "thread_id": "run-1",
                "skill_id": "approval-flow",
                "state_digest": "digest-1",
            },
        )
    )
    scheduler = _StubScheduler()
    ctx = CommandContext(
        **{
            **_ctx(tmp_path, store, scheduler).__dict__,
            "args": {"argv": ("inbox-1", "拒绝原因")},
            "task_store": None,
        }
    )

    reply = await build_builtin_command_registry().resolve("reject_confirmation").handler(ctx)

    assert "流程恢复中" in reply.text
    assert scheduler.payloads
