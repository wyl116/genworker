# edition: baseline
from __future__ import annotations

from pathlib import Path

import pytest

from src.channels.adapters.email_adapter import EmailIMAdapter, EmailPollConfig
from src.channels.dedup import MessageDeduplicator
from src.channels.models import build_channel_binding
from src.channels.registry import IMChannelRegistry
from src.channels.router import ChannelMessageRouter
from src.conversation.session_manager import SessionManager
from src.conversation.session_store import FileSessionStore
from src.events.bus import EventBus
from src.events.models import Event
from src.streaming.events import TextMessageEvent
from src.worker.sensing.sensors.email_sensor import EmailSensor


class StubEmailClient:
    def __init__(self, emails: list[dict] | None = None) -> None:
        self.emails = emails or []
        self.search_calls: list[dict] = []
        self.send_calls: list[dict] = []

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return list(self.emails)

    async def send(self, **kwargs):
        self.send_calls.append(kwargs)
        return f"email-{len(self.send_calls)}"

    async def get_folders(self, **kwargs):
        return ("INBOX",)


class StubWorkerRouter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def route_stream(self, **kwargs):
        self.calls.append(kwargs)
        yield TextMessageEvent(
            run_id=f"run-{len(self.calls)}",
            content=f"已处理: {kwargs['task_context']}",
        )


def _email_binding(**overrides):
    raw = {
        "type": "email",
        "connection_mode": "poll",
        "chat_ids": ["support@corp.com"],
        "reply_mode": "complete",
        "features": {},
    }
    raw.update(overrides)
    return build_channel_binding(raw, tenant_id="demo", worker_id="analyst-01")


@pytest.mark.asyncio
async def test_email_im_full_flow_and_task_completed_notification(tmp_path: Path) -> None:
    email_client = StubEmailClient(emails=[{
        "message_id": "msg-1",
        "from": "Alice <alice@example.com>",
        "to": "support@corp.com",
        "subject": "Need help",
        "content": "请看一下这个问题\n> 历史引用",
        "references": "<root-1> <parent-1>",
        "in_reply_to": "<parent-1>",
    }])
    worker_router = StubWorkerRouter()
    session_manager = SessionManager(FileSessionStore(tmp_path))
    registry = IMChannelRegistry()
    event_bus = EventBus()
    deduplicator = MessageDeduplicator()
    binding = _email_binding()
    adapter = EmailIMAdapter(
        email_client,
        (binding,),
        poll_config=EmailPollConfig(interval_seconds=1, max_fetch_per_poll=10),
    )
    registry.register("email:demo:analyst-01", adapter, chat_ids=("support@corp.com",))
    router = ChannelMessageRouter(
        session_manager=session_manager,
        worker_router=worker_router,
        registry=registry,
        bindings=(binding,),
        event_bus=event_bus,
        deduplicator=deduplicator,
    )

    await adapter.start(router.dispatch)
    await adapter._poll_once()

    thread_id = "im:email:support@corp.com:thread:root-1"
    session = await session_manager.find_by_thread(thread_id)

    assert session is not None
    assert dict(session.metadata)["subject"] == "Need help"
    assert [message.role for message in session.messages] == ["user", "assistant"]
    assert worker_router.calls[0]["task_context"] == "channel_type:email, subject:Need help"
    assert email_client.send_calls[0]["to"] == ("alice@example.com",)
    assert email_client.send_calls[0]["subject"] == "Re: Need help"
    assert email_client.send_calls[0]["reply_to"] == "msg-1"

    await event_bus.publish(Event(
        event_id="evt-1",
        type="task.completed",
        source="test",
        tenant_id="demo",
        payload=(
            ("task_id", "task-1"),
            ("description", "后台处理"),
            ("thread_id", thread_id),
        ),
    ))

    assert email_client.send_calls[1]["to"] == ("alice@example.com",)
    assert email_client.send_calls[1]["subject"] == "Need help"
    assert email_client.send_calls[1]["body"] == "任务已完成: 后台处理"

    await adapter.stop()
    router.close()


@pytest.mark.asyncio
async def test_email_thread_continuity_and_cross_path_dedup(tmp_path: Path) -> None:
    email_client = StubEmailClient(emails=[
        {
            "message_id": "msg-2",
            "from": "alice@example.com",
            "to": "support@corp.com",
            "subject": "Thread A",
            "content": "第一封回复",
            "references": "<root-a>",
        },
        {
            "message_id": "msg-3",
            "from": "alice@example.com",
            "to": "support@corp.com",
            "subject": "Thread A",
            "content": "第二封回复",
            "references": "<root-a> <msg-2>",
        },
        {
            "message_id": "msg-4",
            "from": "alice@example.com",
            "to": "support@corp.com",
            "subject": "Thread A",
            "content": "第三封回复",
            "references": "<root-a> <msg-2> <msg-3>",
        },
    ])
    worker_router = StubWorkerRouter()
    session_manager = SessionManager(FileSessionStore(tmp_path))
    registry = IMChannelRegistry()
    deduplicator = MessageDeduplicator()
    binding = _email_binding()
    adapter = EmailIMAdapter(
        email_client,
        (binding,),
        poll_config=EmailPollConfig(interval_seconds=1, max_fetch_per_poll=10),
    )
    registry.register("email:email_worker", adapter, chat_ids=("support@corp.com",))
    router = ChannelMessageRouter(
        session_manager=session_manager,
        worker_router=worker_router,
        registry=registry,
        bindings=(binding,),
        deduplicator=deduplicator,
    )

    await adapter.start(router.dispatch)
    await adapter._poll_once()

    thread_id = "im:email:support@corp.com:thread:root-a"
    session = await session_manager.find_by_thread(thread_id)

    assert session is not None
    assert len(session.messages) == 6
    assert len(worker_router.calls) == 3

    sensor = EmailSensor(
        email_client=email_client,
        filter_config={},
        deduplicator=deduplicator,
    )
    facts = await sensor.poll()

    assert facts == ()

    await adapter.stop()
    router.close()
