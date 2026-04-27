# edition: baseline
"""Tests for EmailClient."""
from __future__ import annotations

from email.message import EmailMessage
from unittest.mock import MagicMock, patch

import pytest

from src.services.email import (
    EmailAccountConfig,
    EmailClient,
    EmailConfig,
    EmailPermissionError,
)
from src.services.email.client import _extract_attachment_metadata


def _client() -> EmailClient:
    account = EmailAccountConfig(
        address="worker@example.com",
        username="worker",
        password="secret",
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
    )
    owner = EmailAccountConfig(
        address="owner@example.com",
        username="owner",
        password="secret",
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
    )
    return EmailClient(EmailConfig(worker_mailbox=account, owner_mailbox=owner))


@pytest.mark.asyncio
async def test_search_uses_to_thread():
    client = _client()
    with patch("asyncio.to_thread") as to_thread:
        to_thread.return_value = []
        await client.search("hello")
        assert to_thread.called


@pytest.mark.asyncio
async def test_proxy_folder_permission_blocks_drafts():
    client = _client()
    with pytest.raises(EmailPermissionError):
        client._check_folder_permission("Drafts", "proxy_send")


@pytest.mark.asyncio
async def test_send_owner_proxy_uses_owner_account():
    client = _client()
    with patch.object(client, "_send_sync", return_value="email-1") as send_sync:
        result = await client.send(
            to=("a@example.com",),
            subject="Hi",
            body="Body",
            send_mode="proxy_send",
        )
    assert result == "email-1"
    mailbox = send_sync.call_args.args[0]
    assert mailbox.username == "owner"


@pytest.mark.asyncio
async def test_send_passes_cc_and_html_body():
    client = _client()
    with patch.object(client, "_send_sync", return_value="email-1") as send_sync:
        await client.send(
            to=("a@example.com",),
            subject="Hi",
            body="Body",
            cc=("c@example.com",),
            html_body="<p>Body</p>",
        )

    assert send_sync.call_args.args[5] == ("c@example.com",)
    assert send_sync.call_args.args[6] == "<p>Body</p>"


def test_extract_attachment_metadata_reads_attachment_parts():
    message = EmailMessage()
    message.set_content("Body")
    message.add_attachment(
        b"pdf-bytes",
        maintype="application",
        subtype="pdf",
        filename="report.pdf",
    )

    attachments = _extract_attachment_metadata(message)

    assert attachments == [{
        "filename": "report.pdf",
        "content_type": "application/pdf",
        "content_id": "report.pdf",
        "size": len(b"pdf-bytes"),
    }]
