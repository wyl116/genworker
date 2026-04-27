# edition: baseline
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.channels.adapters.slack_adapter import SlackIMAdapter
from src.channels.commands import CommandDispatcher, CommandParser, CommandRegistry, CommandSpec
from src.channels.models import ChannelInboundMessage, ReplyContent, StreamChunk, build_channel_binding
from src.channels.registry import IMChannelRegistry
from src.channels.router import ChannelMessageRouter
from src.common.tenant import Tenant, TenantLoader, TrustLevel


class _StubSlackClient:
    def __init__(self, *, app_token: str = "", signing_secret: str = "secret") -> None:
        self._config = SimpleNamespace(
            bot_token="xoxb-token",
            app_token=app_token,
            signing_secret=signing_secret,
            team_id="T1",
        )
        self.post_message_calls: list[dict] = []
        self.update_message_calls: list[dict] = []
        self.post_blocks_calls: list[dict] = []
        self.response_url_calls: list[tuple[str, dict]] = []
        self.user_info_calls: list[str] = []

    async def post_message(self, channel: str, text: str, *, thread_ts: str | None = None):
        self.post_message_calls.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return {"ts": "100.200"}

    async def update_message(self, channel: str, ts: str, *, text: str | None = None, blocks=None):
        self.update_message_calls.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})
        return {"ts": ts}

    async def post_blocks(self, channel: str, blocks, *, thread_ts: str | None = None):
        self.post_blocks_calls.append({"channel": channel, "blocks": blocks, "thread_ts": thread_ts})
        return {"ts": "200.300"}

    async def post_response_url(self, url: str, payload: dict):
        self.response_url_calls.append((url, payload))
        return {"ok": True}

    async def users_info(self, user_id: str):
        self.user_info_calls.append(user_id)
        return {"user": {"real_name": f"User {user_id}"}}


class _StubSessionManager:
    async def get_or_create(self, **kwargs):
        return SimpleNamespace(messages=(), metadata=tuple((kwargs.get("metadata") or {}).items()))

    async def save(self, session) -> None:
        return None


class _StubWorkerRouter:
    async def route_stream(self, **kwargs):
        if False:
            yield None


class _StubTenantLoader:
    def load(self, tenant_id: str):
        return Tenant(tenant_id=tenant_id, name=tenant_id, trust_level=TrustLevel.BASIC)


def _binding(**overrides):
    raw = {
        "type": "slack",
        "connection_mode": "webhook",
        "chat_ids": ["C123"],
        "reply_mode": "complete",
        "features": {},
    }
    raw.update(overrides)
    return build_channel_binding(raw, tenant_id="demo", worker_id="worker-1")


def _build_signature(secret: str, body: bytes, timestamp: int) -> str:
    base = f"v0:{timestamp}:".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return f"v0={digest}"


class _Request:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self._body = body
        self.headers = headers

    async def body(self) -> bytes:
        return self._body


async def _noop_callback(message: ChannelInboundMessage) -> None:
    return None


def _build_router(registry: IMChannelRegistry, binding) -> ChannelMessageRouter:
    command_registry = CommandRegistry()

    async def _help(ctx):
        return ReplyContent(text="ok")

    command_registry.register(CommandSpec(name="help", description="help", handler=_help))
    return ChannelMessageRouter(
        session_manager=_StubSessionManager(),
        worker_router=_StubWorkerRouter(),
        registry=registry,
        bindings=(binding,),
        tenant_loader=_StubTenantLoader(),
        command_registry=command_registry,
        command_parser=CommandParser(command_registry),
        command_dispatcher=CommandDispatcher(),
    )


@pytest.mark.asyncio
async def test_slack_adapter_start_marks_missing_mode_credentials_as_degraded() -> None:
    socket_adapter = SlackIMAdapter(_StubSlackClient(app_token="", signing_secret="sig"), (_binding(connection_mode="socket_mode"),))
    await socket_adapter.start(_noop_callback)

    assert socket_adapter.status_snapshot()["degraded_reason"] == "missing_slack_app_token"
    assert await socket_adapter.health_check() is False

    webhook_adapter = SlackIMAdapter(_StubSlackClient(app_token="xapp-token", signing_secret=""), (_binding(connection_mode="webhook"),))
    await webhook_adapter.start(_noop_callback)

    assert webhook_adapter.status_snapshot()["degraded_reason"] == "missing_slack_signing_secret"
    assert await webhook_adapter.health_check() is False


@pytest.mark.asyncio
async def test_slack_adapter_health_allows_partial_mode_availability() -> None:
    bindings = (
        _binding(connection_mode="socket_mode"),
        _binding(connection_mode="webhook"),
    )
    adapter = SlackIMAdapter(_StubSlackClient(app_token="", signing_secret="sig"), bindings)

    await adapter.start(_noop_callback)

    assert await adapter.health_check() is True
    snapshot = adapter.status_snapshot()
    assert snapshot["websocket_enabled"] is True
    assert snapshot["socket_mode_enabled"] is True


@pytest.mark.asyncio
async def test_slack_adapter_parse_event_maps_message_payload() -> None:
    adapter = SlackIMAdapter(_StubSlackClient(), (_binding(),))
    payload = {
        "type": "event_callback",
        "team_id": "T1",
        "event_id": "Ev1",
        "event": {
            "type": "message",
            "user": "U1",
            "channel": "C123",
            "channel_type": "im",
            "text": "hi <@U2> <#C999|proj> <!subteam^S1|@ops>",
            "ts": "100.2",
            "thread_ts": "90.1",
            "files": [{"id": "F1", "name": "a.txt", "filetype": "text", "size": 12}],
        },
    }

    message = await adapter.parse_event(payload)

    assert message is not None
    assert message.chat_type == "p2p"
    assert message.sender_name == "User U1"
    assert message.reply_to_id == "90.1"
    assert message.content == "hi @U2 #proj @ops"
    assert message.mentions[0].user_id == "U2"
    assert message.attachments[0].file_key == "F1"


@pytest.mark.asyncio
async def test_slack_adapter_parse_event_marks_app_mention_as_bot_mention() -> None:
    adapter = SlackIMAdapter(_StubSlackClient(), (_binding(),))

    message = await adapter.parse_event({
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "channel": "C123",
            "channel_type": "channel",
            "user": "U1",
            "text": "<@Ubot> hi",
            "ts": "1.1",
        },
    })

    assert message is not None
    assert any(item.is_bot for item in message.mentions)


@pytest.mark.asyncio
async def test_slack_adapter_parse_event_filters_bots_and_unknown_chat() -> None:
    adapter = SlackIMAdapter(_StubSlackClient(), (_binding(chat_ids=["C123"]),))

    bot_message = await adapter.parse_event({
        "type": "event_callback",
        "event": {"type": "message", "channel": "C123", "bot_id": "B1", "ts": "1"},
    })
    other_chat = await adapter.parse_event({
        "type": "event_callback",
        "event": {"type": "message", "channel": "C999", "user": "U1", "ts": "1"},
    })

    assert bot_message is None
    assert other_chat is None


@pytest.mark.asyncio
async def test_slack_adapter_reply_prefers_response_url() -> None:
    client = _StubSlackClient()
    adapter = SlackIMAdapter(client, (_binding(),))
    message = ChannelInboundMessage(
        message_id="100.1",
        channel_type="slack",
        chat_id="C123",
        metadata=(("response_url", "https://example.com/resp"),),
    )

    reply_id = await adapter.reply(message, ReplyContent(text="done"))

    assert reply_id.startswith("slack-response-url:")
    assert client.response_url_calls == [("https://example.com/resp", {"response_type": "in_channel", "text": "done"})]
    assert client.post_message_calls == []


@pytest.mark.asyncio
async def test_slack_adapter_reply_stream_throttles_updates(monkeypatch) -> None:
    client = _StubSlackClient()
    adapter = SlackIMAdapter(client, (_binding(features={"update_interval_ms": 1000}),))
    monkeypatch.setattr("src.channels.adapters.slack_adapter.time.monotonic", lambda: 2.0)

    async def _chunks():
        yield StreamChunk(chunk_type="text_delta", content="A")
        yield StreamChunk(chunk_type="text_delta", content="B")
        yield StreamChunk(chunk_type="finished")

    await adapter.reply_stream(
        ChannelInboundMessage(message_id="100.1", channel_type="slack", chat_id="C123"),
        _chunks(),
    )

    assert len(client.post_message_calls) == 1
    assert len(client.update_message_calls) == 2
    assert client.update_message_calls[-1]["text"] == "AB"


def test_slack_adapter_signature_validation() -> None:
    client = _StubSlackClient(signing_secret="topsecret")
    adapter = SlackIMAdapter(client, (_binding(),))
    body = b'{"type":"event_callback"}'
    timestamp = int(time.time())
    headers = {
        "X-Slack-Request-Timestamp": str(timestamp),
        "X-Slack-Signature": _build_signature("topsecret", body, timestamp),
    }

    assert adapter._verify_signature(body, headers) is True
    assert adapter._verify_signature(body, {**headers, "X-Slack-Signature": "v0=bad"}) is False


@pytest.mark.asyncio
async def test_slack_event_callback_ack_returns_before_callback_awaits() -> None:
    client = _StubSlackClient(signing_secret="topsecret")
    adapter = SlackIMAdapter(client, (_binding(),))

    async def _slow_callback(message: ChannelInboundMessage) -> None:
        await asyncio.sleep(0)

    callback = AsyncMock(side_effect=_slow_callback)
    await adapter.start(callback)
    body = json.dumps({
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "C123",
            "channel_type": "im",
            "user": "U1",
            "text": "hello",
            "ts": "100.2",
        },
    }).encode("utf-8")
    timestamp = int(time.time())
    request = _Request(
        body,
        {
            "X-Slack-Request-Timestamp": str(timestamp),
            "X-Slack-Signature": _build_signature("topsecret", body, timestamp),
        },
    )

    result = await adapter.handle_webhook(request)

    assert result == {"status": "ok"}
    assert callback.await_count == 0
    await asyncio.sleep(0)
    assert callback.await_count == 1
    assert adapter.status_snapshot()["last_event_at"]


@pytest.mark.asyncio
async def test_slack_slash_command_dispatch_uses_router_binding_not_chat_resolution() -> None:
    client = _StubSlackClient(signing_secret="topsecret")
    binding = _binding(chat_ids=["C123"])
    adapter = SlackIMAdapter(client, (binding,))
    registry = IMChannelRegistry()
    registry.register(binding.adapter_id, adapter, chat_ids=binding.chat_ids)
    router = _build_router(registry, binding)
    await adapter.start(router.dispatch)

    body = (
        "token=unused&team_id=T1&channel_id=C999&user_id=U1&user_name=alice"
        "&command=%2Fhelp&text=&response_url=https%3A%2F%2Fexample.com%2Fresp"
        "&trigger_id=1337.42"
    ).encode("utf-8")
    timestamp = int(time.time())
    request = _Request(
        body,
        {
            "X-Slack-Request-Timestamp": str(timestamp),
            "X-Slack-Signature": _build_signature("topsecret", body, timestamp),
        },
    )

    result = await adapter.handle_slash_command(request)
    await asyncio.sleep(0)

    assert result == {"response_type": "in_channel"}
    assert client.response_url_calls == [("https://example.com/resp", {"response_type": "in_channel", "text": "ok"})]


@pytest.mark.asyncio
async def test_slack_socket_mode_reconnects_after_connection_error(monkeypatch) -> None:
    client = _StubSlackClient(app_token="xapp-token", signing_secret="sig")
    adapter = SlackIMAdapter(client, (_binding(connection_mode="socket_mode"),))
    connect_attempts: list[int] = []
    real_sleep = asyncio.sleep

    class _FailingSocketClient:
        def __init__(self, **kwargs) -> None:
            self.socket_mode_request_listeners = []

        async def connect(self) -> None:
            connect_attempts.append(1)
            raise RuntimeError("boom")

        def is_connected(self) -> bool:
            return False

        async def close(self) -> None:
            return None

    async def _fake_sleep(seconds: float) -> None:
        await real_sleep(0)
        if len(connect_attempts) >= 2:
            await adapter.stop()

    class _StubAsyncWebClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr("src.channels.adapters.slack_adapter.SocketModeClient", _FailingSocketClient)
    monkeypatch.setattr("src.channels.adapters.slack_adapter.AsyncWebClient", _StubAsyncWebClient)
    monkeypatch.setattr("src.channels.adapters.slack_adapter.asyncio.sleep", _fake_sleep)

    await adapter.start(_noop_callback)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(connect_attempts) >= 2


@pytest.mark.asyncio
async def test_slack_adapter_marks_socket_mode_degraded_when_sdk_missing(monkeypatch) -> None:
    client = _StubSlackClient(app_token="xapp-token", signing_secret="sig")
    adapter = SlackIMAdapter(client, (_binding(connection_mode="socket_mode"),))

    monkeypatch.setattr("src.channels.adapters.slack_adapter._socket_mode_sdk_available", lambda: False)

    await adapter.start(_noop_callback)

    snapshot = adapter.status_snapshot()
    assert snapshot["degraded_reason"] == "missing_optional_sdk:slack_sdk"
    assert snapshot["connection_state"] == "degraded"


def test_slack_startup_probe_seconds_reads_feature_and_env(monkeypatch) -> None:
    feature_adapter = SlackIMAdapter(
        _StubSlackClient(app_token="xapp-token", signing_secret="sig"),
        (_binding(connection_mode="socket_mode", features={"startup_probe_seconds": 0.25}),),
    )
    assert feature_adapter._startup_probe_seconds() == pytest.approx(0.25)

    monkeypatch.setenv("SLACK_SOCKET_MODE_STARTUP_PROBE_SECONDS", "0.4")
    env_adapter = SlackIMAdapter(
        _StubSlackClient(app_token="xapp-token", signing_secret="sig"),
        (_binding(connection_mode="socket_mode"),),
    )
    assert env_adapter._startup_probe_seconds() == pytest.approx(0.4)
