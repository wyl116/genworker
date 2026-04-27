# edition: baseline
from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.channels.outbound_types import ChannelMessage, SenderScope
from src.worker.integrations.worker_scoped_channel_gateway import (
    WorkerScopedChannelGateway,
)


class _StubEmailClient:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict] = []

    async def send(self, **kwargs):
        self.calls.append(kwargs)
        return f"email-{self.name}"


class _StubFeishuClient:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict] = []

    async def send_message(self, recipients, content):
        self.calls.append({"recipients": tuple(recipients), "content": content})
        return {"message_id": f"feishu-{self.name}"}


class _StubSlackClient:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict] = []

    async def post_message(self, channel: str, text: str):
        self.calls.append({"channel": channel, "text": text})
        return {"ts": f"slack-{self.name}"}


class _StubMountManager:
    def __init__(self) -> None:
        self.contents: dict[str, str] = {}

    async def read_file(self, path: str) -> str:
        return self.contents.get(path, "")

    async def write_file(self, path: str, content: str) -> None:
        self.contents[path] = content


class _StubPlatformClientFactory:
    def __init__(self, mapping: dict[tuple[str, str, str], object]) -> None:
        self.mapping = mapping

    def get_client(self, tenant_id: str, worker_id: str, channel_type: str):
        return self.mapping.get((tenant_id, worker_id, channel_type))


class _StubRegistry:
    def __init__(self, adapter=None) -> None:
        self._adapter = adapter

    def find_by_chat_id(self, chat_id: str):
        return self._adapter


class _StubIMAdapter:
    channel_type = "email"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, content) -> str:
        self.calls.append((chat_id, content.text))
        return "im-msg"


@pytest.mark.asyncio
async def test_gateway_sends_email_with_worker_scoped_client() -> None:
    alice_client = _StubEmailClient("alice")
    bob_client = _StubEmailClient("bob")
    gateway = WorkerScopedChannelGateway(
        platform_client_factory=_StubPlatformClientFactory({
            ("demo", "alice", "email"): alice_client,
            ("demo", "bob", "email"): bob_client,
        }),
        mount_manager=_StubMountManager(),
        tool_executor=None,
        im_channel_registry=None,
        event_bus=None,
    )

    message_id = await gateway.send(ChannelMessage(
        channel="email",
        recipients=("user@example.com",),
        subject="Status",
        content="hello",
        sender_tenant_id="demo",
        sender_worker_id="alice",
    ))

    assert message_id == "email-alice"
    assert len(alice_client.calls) == 1
    assert bob_client.calls == []


@pytest.mark.asyncio
async def test_gateway_sends_feishu_with_worker_scoped_client() -> None:
    feishu_client = _StubFeishuClient("alice")
    gateway = WorkerScopedChannelGateway(
        platform_client_factory=_StubPlatformClientFactory({
            ("demo", "alice", "feishu"): feishu_client,
        }),
        mount_manager=_StubMountManager(),
        tool_executor=None,
        im_channel_registry=None,
        event_bus=None,
    )

    message_id = await gateway.send(ChannelMessage(
        channel="feishu",
        recipients=("u1",),
        subject="Status",
        content="hello",
        sender_tenant_id="demo",
        sender_worker_id="alice",
    ))

    assert message_id.startswith("feishu-")
    assert feishu_client.calls == [{"recipients": ("u1",), "content": "hello"}]


@pytest.mark.asyncio
async def test_gateway_requires_sender_scope_for_direct_send() -> None:
    gateway = WorkerScopedChannelGateway(
        platform_client_factory=_StubPlatformClientFactory({}),
        mount_manager=_StubMountManager(),
        tool_executor=None,
        im_channel_registry=None,
        event_bus=None,
    )

    with pytest.raises(RuntimeError, match="email send requires sender scope"):
        await gateway.send(ChannelMessage(
            channel="email",
            recipients=("user@example.com",),
            subject="Status",
            content="hello",
        ))


@pytest.mark.asyncio
async def test_gateway_prefers_im_registry_for_chat_replies() -> None:
    adapter = _StubIMAdapter()
    gateway = WorkerScopedChannelGateway(
        platform_client_factory=_StubPlatformClientFactory({}),
        mount_manager=_StubMountManager(),
        tool_executor=None,
        im_channel_registry=_StubRegistry(adapter),
        event_bus=None,
    )

    message_id = await gateway.send(ChannelMessage(
        channel="email",
        recipients=("user@example.com",),
        subject="Status",
        content="hello",
        im_chat_id="thread-1",
    ))

    assert message_id == "im-msg"
    assert adapter.calls == [("thread-1", "hello")]


@pytest.mark.asyncio
async def test_gateway_sends_slack_with_worker_scoped_client() -> None:
    slack_client = _StubSlackClient("alice")
    gateway = WorkerScopedChannelGateway(
        platform_client_factory=_StubPlatformClientFactory({
            ("demo", "alice", "slack"): slack_client,
        }),
        mount_manager=_StubMountManager(),
        tool_executor=None,
        im_channel_registry=None,
        event_bus=None,
    )

    message_id = await gateway.send(ChannelMessage(
        channel="slack",
        recipients=("C123",),
        subject="",
        content="hello",
        sender_tenant_id="demo",
        sender_worker_id="alice",
    ))

    assert message_id == "slack-alice"
    assert slack_client.calls == [{"channel": "C123", "text": "hello"}]
