# edition: baseline
from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from src.services.email import EmailAccountConfig, EmailClient, EmailConfig
from src.tools.builtin.email_tools import (
    create_email_download_attachment_tool,
    create_email_search_tool,
    create_email_send_tool,
    create_worker_scoped_email_send_tool,
)
from src.tools.runtime_scope import ExecutionScope, ExecutionScopeProvider


class StubEmailClient:
    def __init__(self) -> None:
        self.search_calls: list[dict] = []
        self.send_calls: list[dict] = []
        self.download_calls: list[dict] = []

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return [{"message_id": "m1", "subject": "hello"}]

    async def send(self, **kwargs):
        self.send_calls.append(kwargs)
        return "msg-1"

    async def download_attachment(self, **kwargs):
        self.download_calls.append(kwargs)
        return b"payload"


class ScopedStubEmailClient(EmailClient):
    def __init__(self) -> None:
        super().__init__(EmailConfig(
            worker_mailbox=EmailAccountConfig(),
            owner_mailbox=EmailAccountConfig(),
        ))
        self.send_calls: list[dict] = []

    async def send(self, **kwargs):
        self.send_calls.append(kwargs)
        return "msg-1"


@pytest.mark.asyncio
async def test_email_search_tool_returns_structured_emails() -> None:
    client = StubEmailClient()
    tool = create_email_search_tool(client)

    result = await tool.handler(query="hello", account="worker_mailbox", folder="INBOX")

    assert result == {"emails": [{"message_id": "m1", "subject": "hello"}]}
    assert client.search_calls == [{
        "query": "hello",
        "account": "worker_mailbox",
        "folder": "INBOX",
    }]


@pytest.mark.asyncio
async def test_email_send_tool_splits_addresses_and_returns_message_id() -> None:
    client = StubEmailClient()
    tool = create_email_send_tool(client)

    result = await tool.handler(
        to="a@example.com, b@example.com",
        cc="c@example.com",
        subject="Hi",
        body="Body",
        reply_to="orig-1",
        html_body="<p>Body</p>",
    )

    assert result == {"status": "sent", "message_id": "msg-1"}
    assert client.send_calls == [{
        "to": ("a@example.com", "b@example.com"),
        "subject": "Hi",
        "body": "Body",
        "reply_to": "orig-1",
        "send_mode": "worker_mailbox",
        "cc": ("c@example.com",),
        "html_body": "<p>Body</p>",
    }]


@pytest.mark.asyncio
async def test_email_download_attachment_tool_returns_base64() -> None:
    client = StubEmailClient()
    tool = create_email_download_attachment_tool(client)

    result = await tool.handler(
        message_id="msg-1",
        content_id="report.pdf",
        account="worker_mailbox",
        folder="INBOX",
    )

    assert result["message_id"] == "msg-1"
    assert result["content_id"] == "report.pdf"
    assert base64.b64decode(result["content_base64"]) == b"payload"
    assert result["size"] == len(b"payload")
    assert client.download_calls == [{
        "message_id": "msg-1",
        "content_id": "report.pdf",
        "account": "worker_mailbox",
        "folder": "INBOX",
    }]


@pytest.mark.asyncio
async def test_worker_scoped_email_send_tool_uses_current_scope() -> None:
    client = ScopedStubEmailClient()
    scope_provider = ExecutionScopeProvider()

    class _Factory:
        def get_client(self, tenant_id: str, worker_id: str, channel_type: str):
            assert tenant_id == "demo"
            assert worker_id == "alice"
            assert channel_type == "email"
            return client

    tool = create_worker_scoped_email_send_tool(_Factory(), scope_provider)

    async with scope_provider.use(ExecutionScope(
        tenant_id="demo",
        worker_id="alice",
        skill_id="skill-1",
        trust_gate=SimpleNamespace(),
        allowed_tool_names=frozenset({"email_send"}),
    )):
        result = await tool.handler(
            to="a@example.com",
            subject="Hi",
            body="Body",
        )

    assert result == {"status": "sent", "message_id": "msg-1"}
    assert client.send_calls[0]["to"] == ("a@example.com",)


@pytest.mark.asyncio
async def test_worker_scoped_email_send_tool_requires_scope() -> None:
    class _Factory:
        def get_client(self, tenant_id: str, worker_id: str, channel_type: str):
            return None

    tool = create_worker_scoped_email_send_tool(_Factory(), ExecutionScopeProvider())

    with pytest.raises(RuntimeError, match="email_send requires execution scope"):
        await tool.handler(
            to="a@example.com",
            subject="Hi",
            body="Body",
        )
