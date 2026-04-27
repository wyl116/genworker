"""Email IM adapter implementation."""
from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from email.utils import getaddresses, parseaddr
from typing import Any, AsyncGenerator

from src.common.logger import get_logger
from src.services.email.client import EmailClient

from ..models import (
    Attachment,
    ChannelBinding,
    ChannelInboundMessage,
    Mention,
    ReplyContent,
    StreamChunk,
    freeze_data,
)
from ..protocol import MessageCallback

logger = get_logger()


@dataclass(frozen=True)
class EmailPollConfig:
    """Polling configuration for the email channel."""

    interval_seconds: int = 60
    max_fetch_per_poll: int = 50
    folders: tuple[str, ...] = ("INBOX",)
    account: str = "worker_mailbox"


class EmailIMAdapter:
    """Polling-based bidirectional email adapter."""

    channel_type = "email"

    def __init__(
        self,
        client: EmailClient,
        bindings: tuple[ChannelBinding, ...],
        poll_config: EmailPollConfig = EmailPollConfig(),
    ) -> None:
        self._client = client
        self._bindings = bindings
        self._poll_config = poll_config
        self._message_callback: MessageCallback | None = None
        self._started = False
        self._poll_task: asyncio.Task[Any] | None = None
        self._seen_message_ids: set[str] = set()
        self._chat_recipients: dict[str, str] = {}
        self._chat_ids = tuple(
            dict.fromkeys(
                chat_id
                for binding in bindings
                for chat_id in binding.chat_ids
                if chat_id
            )
        )
        self._last_poll_at = ""
        self._last_error = ""

    def supports_streaming(self) -> bool:
        return False

    async def start(self, message_callback: MessageCallback) -> None:
        self._message_callback = message_callback
        self._started = True
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            "[EmailIMAdapter] Started polling interval=%ss folders=%s",
            self._poll_config.interval_seconds,
            ",".join(self._poll_config.folders),
        )

    async def stop(self) -> None:
        self._started = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        logger.info("[EmailIMAdapter] Stopped")

    async def health_check(self) -> bool:
        try:
            folders = await self._client.get_folders(account=self._poll_config.account)
        except Exception as exc:
            self._last_error = str(exc)
            logger.warning("[EmailIMAdapter] Health check failed: %s", exc)
            return False
        return bool(folders)

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "channel_type": self.channel_type,
            "started": self._started,
            "healthy": self._started and not self._last_error,
            "connection_state": "polling" if self._started else "stopped",
            "folders": list(self._poll_config.folders),
            "account": self._poll_config.account,
            "last_poll_at": self._last_poll_at,
            "last_error": self._last_error,
        }

    async def parse_event(self, raw_event: Any) -> ChannelInboundMessage | None:
        if not isinstance(raw_event, dict):
            return None

        message_id = _normalize_message_ref(raw_event.get("message_id", ""))
        if not message_id:
            return None

        sender_header = str(raw_event.get("from", "")).strip()
        sender_name, sender_address = parseaddr(sender_header)
        sender_id = sender_address or sender_header
        if not sender_id:
            return None

        subject = str(raw_event.get("subject", "")).strip()
        raw_content = str(raw_event.get("content", "")).strip()
        cc_raw = str(raw_event.get("cc", "")).strip()
        in_reply_to = _normalize_message_ref(raw_event.get("in_reply_to", ""))
        references = str(raw_event.get("references", "")).strip()

        thread_root = _extract_thread_root(references, message_id)
        mentions = _parse_cc_as_mentions(cc_raw)
        attachments = _parse_attachments(raw_event.get("attachments", ()))
        content = _clean_email_body(raw_content)
        chat_id = self._resolve_chat_id(raw_event)
        if not chat_id:
            logger.debug("[EmailIMAdapter] No matching chat_id for raw event")
            return None

        reply_to_id = thread_root if thread_root and thread_root != message_id else (in_reply_to or None)
        text = f"[{subject}]\n{content}".strip() if subject else content

        return ChannelInboundMessage(
            message_id=message_id,
            channel_type=self.channel_type,
            chat_id=chat_id,
            chat_type="p2p",
            sender_id=sender_id,
            sender_name=sender_name or _extract_display_name(sender_id),
            content=text,
            msg_type="text",
            reply_to_id=reply_to_id,
            mentions=mentions,
            attachments=attachments,
            raw_event=freeze_data(raw_event),
            metadata=freeze_data({
                "subject": subject,
                "references": references,
                "thread_root": thread_root,
                "in_reply_to": in_reply_to,
            }),
        )

    async def reply(
        self,
        source_msg: ChannelInboundMessage,
        content: ReplyContent,
    ) -> str:
        metadata = source_msg.metadata_dict
        subject = str(metadata.get("subject", "")).strip()
        if subject and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        html_body = ""
        body = content.text
        if content.content_type == "card":
            html_body = _card_to_html(content.card_dict)
            if not body:
                body = _card_to_text(content.card_dict)
        elif "\n" in content.text:
            html_body = _markdown_to_html(content.text)

        return await self._client.send(
            to=(source_msg.sender_id,),
            subject=subject,
            body=body,
            reply_to=source_msg.message_id,
            html_body=html_body,
        )

    async def reply_stream(
        self,
        source_msg: ChannelInboundMessage,
        chunks: AsyncGenerator[StreamChunk, None],
    ) -> str:
        parts: list[str] = []
        async for chunk in chunks:
            if chunk.chunk_type == "text_delta" and chunk.content:
                parts.append(chunk.content)
        return await self.reply(source_msg, ReplyContent(text="".join(parts)))

    async def send_message(
        self,
        chat_id: str,
        content: ReplyContent,
    ) -> str:
        metadata = dict(content.metadata)
        recipient = str(metadata.get("recipient", "")).strip()
        if not recipient:
            recipient = self._chat_recipients.get(chat_id, "")
        if not recipient:
            logger.warning(
                "[EmailIMAdapter] Cannot resolve recipient for proactive send chat_id=%s",
                chat_id,
            )
            return ""

        subject = str(metadata.get("subject", "")).strip()
        html_body = ""
        body = content.text
        if content.content_type == "card":
            html_body = _card_to_html(content.card_dict)
            if not body:
                body = _card_to_text(content.card_dict)
        elif "\n" in content.text:
            html_body = _markdown_to_html(content.text)

        return await self._client.send(
            to=(recipient,),
            subject=subject,
            body=body,
            html_body=html_body,
        )

    async def handle_webhook(self, request: Any) -> Any:
        return {"status": "not_supported"}

    def _resolve_chat_id(self, raw_event: dict[str, Any]) -> str:
        if len(self._chat_ids) == 1:
            return self._chat_ids[0]
        to_header = str(raw_event.get("to", "")).lower()
        for _, address in getaddresses([to_header]):
            normalized = address.strip().lower()
            for chat_id in self._chat_ids:
                if chat_id.lower() == normalized:
                    return chat_id
        return self._chat_ids[0] if self._chat_ids else ""

    async def _poll_loop(self) -> None:
        while self._started:
            try:
                await self._poll_once()
                self._last_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                logger.error("[EmailIMAdapter] Poll failed: %s", exc, exc_info=True)
            await asyncio.sleep(self._poll_config.interval_seconds)

    async def _poll_once(self) -> None:
        if self._message_callback is None:
            return
        self._last_poll_at = asyncio.get_running_loop().time().__str__()
        for folder in self._poll_config.folders:
            emails = await self._client.search(
                account=self._poll_config.account,
                folder=folder,
            )
            for raw_email in emails[-self._poll_config.max_fetch_per_poll:]:
                msg_id = _normalize_message_ref(raw_email.get("message_id", ""))
                if not msg_id or msg_id in self._seen_message_ids:
                    continue
                self._seen_message_ids.add(msg_id)
                message = await self.parse_event(raw_email)
                if message is None:
                    continue
                if message.chat_id and message.sender_id:
                    self._chat_recipients[message.chat_id] = message.sender_id
                await self._message_callback(message)


def _extract_thread_root(references: str, fallback: str) -> str:
    parts = [
        _normalize_message_ref(part)
        for part in str(references or "").split()
        if _normalize_message_ref(part)
    ]
    return parts[0] if parts else _normalize_message_ref(fallback)


def _parse_cc_as_mentions(cc_raw: str) -> tuple[Mention, ...]:
    mentions: list[Mention] = []
    for name, address in getaddresses([cc_raw]):
        address = address.strip()
        if not address:
            continue
        mentions.append(Mention(
            user_id=address,
            name=(name or _extract_display_name(address)).strip(),
        ))
    return tuple(mentions)


def _extract_display_name(email_addr: str) -> str:
    name, address = parseaddr(str(email_addr or ""))
    if name:
        return name.strip().strip('"')
    if address and "@" in address:
        return address.split("@", 1)[0]
    return str(email_addr or "").strip()


def _parse_attachments(raw_attachments: Any) -> tuple[Attachment, ...]:
    if not isinstance(raw_attachments, (list, tuple)):
        return ()
    attachments: list[Attachment] = []
    for item in raw_attachments:
        if not isinstance(item, dict):
            continue
        attachments.append(Attachment(
            file_key=str(item.get("content_id", item.get("filename", ""))),
            file_name=str(item.get("filename", "")),
            file_type=str(item.get("content_type", "file")),
            size=int(item.get("size", 0) or 0),
        ))
    return tuple(attachments)


def _clean_email_body(raw_body: str) -> str:
    cleaned: list[str] = []
    for line in str(raw_body or "").splitlines():
        stripped = line.strip()
        if stripped == "--":
            break
        if stripped.startswith(">"):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _markdown_to_html(text: str) -> str:
    lines = str(text or "").splitlines()
    html_parts: list[str] = []
    in_code_block = False
    in_list = False

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        if line.startswith("```"):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append("<pre><code>" if not in_code_block else "</code></pre>")
            in_code_block = not in_code_block
            continue
        if in_code_block:
            html_parts.append(html.escape(line))
            continue

        escaped = html.escape(line)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"\*(.+?)\*", r"<em>\1</em>", escaped)
        escaped = re.sub(r"`(.+?)`", r"<code>\1</code>", escaped)

        if escaped.startswith("- "):
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            html_parts.append(f"<li>{escaped[2:]}</li>")
            continue

        if in_list:
            html_parts.append("</ul>")
            in_list = False

        html_parts.append(f"<p>{escaped}</p>" if escaped.strip() else "<br/>")

    if in_list:
        html_parts.append("</ul>")

    body = "\n".join(html_parts)
    return f'<div style="font-family: sans-serif; line-height: 1.6;">{body}</div>'


def _card_to_html(card: dict[str, Any]) -> str:
    title = html.escape(str(card.get("title", "")).strip())
    sections = card.get("sections", ())
    parts: list[str] = []
    if title:
        parts.append(f'<h2 style="color: #333;">{title}</h2>')
    for section in sections:
        if not isinstance(section, dict):
            continue
        header = html.escape(str(section.get("header", "")).strip())
        content = html.escape(str(section.get("content", "")).strip())
        if header:
            parts.append(f'<h3 style="color: #555;">{header}</h3>')
        if content:
            parts.append(f"<p>{content}</p>")
    return f'<div style="font-family: sans-serif; max-width: 600px;">{"".join(parts)}</div>'


def _card_to_text(card: dict[str, Any]) -> str:
    parts: list[str] = []
    title = str(card.get("title", "")).strip()
    if title:
        parts.append(title)
    for section in card.get("sections", ()):
        if not isinstance(section, dict):
            continue
        header = str(section.get("header", "")).strip()
        content = str(section.get("content", "")).strip()
        if header:
            parts.append(header)
        if content:
            parts.append(content)
    return "\n\n".join(parts)


def _normalize_message_ref(value: Any) -> str:
    return str(value or "").strip().strip("<>").strip()
