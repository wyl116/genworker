# edition: baseline
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from types import SimpleNamespace

import pytest

from src.channels.adapters.slack_adapter import SlackIMAdapter
from src.channels.commands import CommandDispatcher, CommandParser, CommandRegistry, CommandSpec
from src.channels.models import ReplyContent, build_channel_binding
from src.channels.registry import IMChannelRegistry
from src.channels.router import ChannelMessageRouter
from src.common.tenant import Tenant, TrustLevel


class _StubSlackClient:
    def __init__(self) -> None:
        self._config = SimpleNamespace(
            bot_token="xoxb-token",
            app_token="",
            signing_secret="topsecret",
            team_id="T1",
        )
        self.response_url_calls: list[tuple[str, dict]] = []

    async def post_message(self, channel: str, text: str, *, thread_ts: str | None = None):
        return {"ts": "100.1"}

    async def post_blocks(self, channel: str, blocks, *, thread_ts: str | None = None):
        return {"ts": "100.1"}

    async def update_message(self, channel: str, ts: str, *, text: str | None = None, blocks=None):
        return {"ts": ts}

    async def post_response_url(self, url: str, payload: dict):
        self.response_url_calls.append((url, payload))
        return {"ok": True}

    async def users_info(self, user_id: str):
        return {"user": {"real_name": user_id}}


class _SessionManager:
    async def get_or_create(self, **kwargs):
        return SimpleNamespace(messages=(), metadata=tuple((kwargs.get("metadata") or {}).items()))

    async def save(self, session) -> None:
        return None


class _WorkerRouter:
    async def route_stream(self, **kwargs):
        if False:
            yield None


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
            "chat_ids": ["C123"],
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
async def test_slack_slash_command_dispatch_roundtrip() -> None:
    binding = _binding()
    client = _StubSlackClient()
    adapter = SlackIMAdapter(client, (binding,))
    registry = IMChannelRegistry()
    registry.register(binding.adapter_id, adapter, chat_ids=binding.chat_ids)

    command_registry = CommandRegistry()

    async def _help(ctx):
        return ReplyContent(text="slash ok")

    command_registry.register(CommandSpec(name="help", description="help", handler=_help))
    router = ChannelMessageRouter(
        session_manager=_SessionManager(),
        worker_router=_WorkerRouter(),
        registry=registry,
        bindings=(binding,),
        tenant_loader=_TenantLoader(),
        command_registry=command_registry,
        command_parser=CommandParser(command_registry),
        command_dispatcher=CommandDispatcher(),
    )
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
    assert client.response_url_calls == [
        ("https://example.com/resp", {"response_type": "in_channel", "text": "slash ok"}),
    ]
