# edition: baseline
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest

from src.channels.adapters.slack_adapter import SlackIMAdapter
from src.channels.models import ReplyContent, build_channel_binding
from src.channels.registry import IMChannelRegistry
from src.channels.router import ChannelMessageRouter
from src.common.tenant import Tenant, TrustLevel
from src.streaming.events import TextMessageEvent


class _StubSlackClient:
    def __init__(self) -> None:
        self._config = SimpleNamespace(
            bot_token="xoxb-token",
            app_token="",
            signing_secret="topsecret",
            team_id="T1",
        )
        self.post_message_calls: list[dict] = []
        self.response_url_calls: list[tuple[str, dict]] = []

    async def post_message(self, channel: str, text: str, *, thread_ts: str | None = None):
        self.post_message_calls.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return {"ts": "200.1"}

    async def post_blocks(self, channel: str, blocks, *, thread_ts: str | None = None):
        return {"ts": "300.1"}

    async def update_message(self, channel: str, ts: str, *, text: str | None = None, blocks=None):
        return {"ts": ts}

    async def post_response_url(self, url: str, payload: dict):
        self.response_url_calls.append((url, payload))
        return {"ok": True}

    async def users_info(self, user_id: str):
        return {"user": {"real_name": f"User {user_id}"}}


class _SessionManager:
    async def get_or_create(self, **kwargs):
        return SimpleNamespace(messages=(), metadata=tuple((kwargs.get("metadata") or {}).items()), append_message=lambda msg: SimpleNamespace(messages=(msg,), metadata=tuple((kwargs.get("metadata") or {}).items())))

    async def save(self, session) -> None:
        return None

    async def find_by_thread(self, thread_id: str):
        return None


class _WorkerRouter:
    async def route_stream(self, **kwargs):
        yield TextMessageEvent(run_id="run-1", content="收到 Slack 消息")


class _TenantLoader:
    def load(self, tenant_id: str):
        return Tenant(tenant_id=tenant_id, name=tenant_id, trust_level=TrustLevel.BASIC)


class _Request:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self._body = body
        self.headers = headers

    async def body(self) -> bytes:
        return self._body


def _binding():
    return build_channel_binding(
        {
            "type": "slack",
            "connection_mode": "webhook",
            "chat_ids": ["D123"],
            "reply_mode": "complete",
            "features": {},
        },
        tenant_id="demo",
        worker_id="worker-1",
    )


def _build_signature(secret: str, body: bytes, timestamp: int) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        f"v0:{timestamp}:".encode("utf-8") + body,
        hashlib.sha256,
    ).hexdigest()
    return f"v0={digest}"


@pytest.mark.asyncio
async def test_slack_webhook_roundtrip_dispatches_and_replies() -> None:
    binding = _binding()
    client = _StubSlackClient()
    adapter = SlackIMAdapter(client, (binding,))
    registry = IMChannelRegistry()
    registry.register(binding.adapter_id, adapter, chat_ids=binding.chat_ids)
    router = ChannelMessageRouter(
        session_manager=_SessionManager(),
        worker_router=_WorkerRouter(),
        registry=registry,
        bindings=(binding,),
        tenant_loader=_TenantLoader(),
    )
    await adapter.start(router.dispatch)

    body = json.dumps({
        "type": "event_callback",
        "team_id": "T1",
        "event": {
            "type": "message",
            "channel": "D123",
            "channel_type": "im",
            "user": "U1",
            "text": "你好",
            "ts": "100.1",
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
    await asyncio.sleep(0)

    assert result == {"status": "ok"}
    assert client.post_message_calls == [{
        "channel": "D123",
        "text": "收到 Slack 消息",
        "thread_ts": "100.1",
    }]
