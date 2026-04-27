# edition: baseline
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.channels.adapters.email_adapter import (
    EmailIMAdapter,
    EmailPollConfig,
    _card_to_html,
    _clean_email_body,
    _extract_thread_root,
    _markdown_to_html,
)
from src.channels.models import ReplyContent, build_channel_binding


class StubEmailClient:
    def __init__(self, emails: list[dict] | None = None) -> None:
        self.emails = emails or []
        self.send_calls: list[dict] = []
        self.search_calls: list[dict] = []
        self.folder_calls: list[dict] = []

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return list(self.emails)

    async def send(self, **kwargs):
        self.send_calls.append(kwargs)
        return "email-1"

    async def get_folders(self, **kwargs):
        self.folder_calls.append(kwargs)
        return ("INBOX",)


def _binding(**overrides):
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
async def test_parse_event_maps_email_fields_and_thread_root() -> None:
    client = StubEmailClient()
    adapter = EmailIMAdapter(client, (_binding(),))

    message = await adapter.parse_event({
        "message_id": "<msg-1>",
        "from": "Alice <alice@example.com>",
        "to": "support@corp.com",
        "cc": "Bob <bob@example.com>, carol@example.com",
        "subject": "Need help",
        "content": "你好\n> old quote\n--\nAlice",
        "in_reply_to": "<parent-1>",
        "references": "<root-1> <parent-1>",
        "attachments": [{
            "filename": "report.pdf",
            "content_type": "application/pdf",
            "content_id": "cid-1",
            "size": 128,
        }],
    })

    assert message is not None
    assert message.chat_id == "support@corp.com"
    assert message.sender_id == "alice@example.com"
    assert message.reply_to_id == "root-1"
    assert message.content == "[Need help]\n你好"
    assert [mention.user_id for mention in message.mentions] == [
        "bob@example.com",
        "carol@example.com",
    ]
    assert message.attachments[0].file_name == "report.pdf"
    assert message.metadata_dict["thread_root"] == "root-1"


@pytest.mark.asyncio
async def test_parse_event_matches_to_header_for_multi_mailbox() -> None:
    client = StubEmailClient()
    adapter = EmailIMAdapter(
        client,
        (_binding(chat_ids=["support@corp.com", "sales@corp.com"]),),
    )

    message = await adapter.parse_event({
        "message_id": "msg-2",
        "from": "alice@example.com",
        "to": "Sales <sales@corp.com>",
        "subject": "Quote",
        "content": "hello",
    })

    assert message is not None
    assert message.chat_id == "sales@corp.com"


@pytest.mark.asyncio
async def test_reply_generates_re_subject_and_html_for_multiline_text() -> None:
    client = StubEmailClient()
    adapter = EmailIMAdapter(client, (_binding(),))
    message = await adapter.parse_event({
        "message_id": "msg-3",
        "from": "alice@example.com",
        "to": "support@corp.com",
        "subject": "Status",
        "content": "hello",
    })

    await adapter.reply(message, ReplyContent(text="第一行\n- 第二行"))

    assert client.send_calls[0]["subject"] == "Re: Status"
    assert client.send_calls[0]["reply_to"] == "msg-3"
    assert "<ul>" in client.send_calls[0]["html_body"]


@pytest.mark.asyncio
async def test_send_message_prefers_metadata_recipient_then_recent_sender() -> None:
    client = StubEmailClient()
    adapter = EmailIMAdapter(client, (_binding(),))

    await adapter.send_message(
        "support@corp.com",
        ReplyContent(
            text="done",
            metadata=(("recipient", "alice@example.com"), ("subject", "Re: Task")),
        ),
    )
    assert client.send_calls[0]["to"] == ("alice@example.com",)
    assert client.send_calls[0]["subject"] == "Re: Task"

    adapter._chat_recipients["support@corp.com"] = "bob@example.com"
    await adapter.send_message("support@corp.com", ReplyContent(text="fallback"))
    assert client.send_calls[1]["to"] == ("bob@example.com",)


@pytest.mark.asyncio
async def test_poll_once_dispatches_new_messages_and_tracks_recent_sender() -> None:
    client = StubEmailClient(emails=[{
        "message_id": "msg-4",
        "from": "alice@example.com",
        "to": "support@corp.com",
        "subject": "Hello",
        "content": "Ping",
    }])
    adapter = EmailIMAdapter(
        client,
        (_binding(),),
        poll_config=EmailPollConfig(interval_seconds=1, max_fetch_per_poll=10),
    )
    captured = []

    async def _callback(message):
        captured.append(message)

    await adapter.start(_callback)
    await adapter._poll_once()
    await adapter.stop()

    assert len(captured) == 1
    assert adapter._chat_recipients["support@corp.com"] == "alice@example.com"


def test_markdown_to_html_escapes_script_and_wraps_lists() -> None:
    rendered = _markdown_to_html("Hello<script>\n- one\n- two\n```py\nx<1\n```")

    assert "&lt;script&gt;" in rendered
    assert "<ul>" in rendered and "</ul>" in rendered
    assert "<pre><code>" in rendered
    assert "x&lt;1" in rendered


def test_card_to_html_escapes_values() -> None:
    rendered = _card_to_html({
        "title": "<script>",
        "sections": [{"header": "A&B", "content": "<b>unsafe</b>"}],
    })

    assert "&lt;script&gt;" in rendered
    assert "A&amp;B" in rendered
    assert "&lt;b&gt;unsafe&lt;/b&gt;" in rendered


def test_email_adapter_helpers_normalize_thread_and_body() -> None:
    assert _extract_thread_root("<root-1> <reply-1>", "fallback") == "root-1"
    assert _clean_email_body("line1\n> quoted\n--\nsig") == "line1"
