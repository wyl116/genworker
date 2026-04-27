"""Async email client built on stdlib IMAP/SMTP libraries."""
from __future__ import annotations

import asyncio
import email
import imaplib
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid, parsedate_to_datetime
from typing import Any

from .config import EmailAccountConfig, EmailConfig
from .exceptions import EmailClientError, EmailPermissionError


class EmailClient:
    """Email client supporting worker/owner/proxy send modes."""

    def __init__(self, config: EmailConfig) -> None:
        self._config = config

    async def search(
        self,
        query: str = "",
        *,
        account: str = "worker_mailbox",
        folder: str = "INBOX",
    ) -> list[dict[str, Any]]:
        mailbox = self._get_account(account)
        self._check_folder_permission(folder, account)
        return await asyncio.to_thread(self._search_sync, mailbox, query, folder)

    async def send(
        self,
        *,
        to: tuple[str, ...] | list[str],
        subject: str,
        body: str,
        send_mode: str = "worker_mailbox",
        reply_to: str | None = None,
        cc: tuple[str, ...] | list[str] = (),
        html_body: str = "",
    ) -> str:
        sender = self._account_for_send_mode(send_mode)
        return await asyncio.to_thread(
            self._send_sync,
            sender,
            tuple(to),
            subject,
            body,
            reply_to,
            tuple(cc),
            html_body,
        )

    async def get_folders(
        self,
        *,
        account: str = "worker_mailbox",
        include_restricted: bool = False,
    ) -> tuple[str, ...]:
        mailbox = self._get_account(account)
        folders = await asyncio.to_thread(self._list_folders_sync, mailbox)
        if not include_restricted and account == "proxy_send":
            allowed = []
            for folder in folders:
                self._check_folder_permission(folder, account)
                allowed.append(folder)
            return tuple(allowed)
        return tuple(folders)

    def _check_folder_permission(self, folder: str, account: str) -> None:
        if account != "proxy_send":
            return
        normalized = folder.lower()
        if normalized not in {"inbox", "sent", "sent items"}:
            raise EmailPermissionError(
                f"Proxy mailbox cannot access folder: {folder}"
            )

    def _search_sync(
        self,
        mailbox: EmailAccountConfig,
        query: str,
        folder: str,
    ) -> list[dict[str, Any]]:
        client = imaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port)
        try:
            client.login(mailbox.username, mailbox.password)
            client.select(folder)
            status, data = client.search(None, "ALL")
            if status != "OK":
                return []
            message_ids = data[0].split()
            results: list[dict[str, Any]] = []
            for message_id in message_ids:
                _, raw_parts = client.fetch(message_id, "(RFC822)")
                raw_email = raw_parts[0][1]
                msg = email.message_from_bytes(raw_email)
                item = {
                    "from": msg.get("From", ""),
                    "to": msg.get("To", ""),
                    "cc": msg.get("Cc", ""),
                    "subject": msg.get("Subject", ""),
                    "content": _extract_email_body(msg),
                    "date": _parse_email_date(msg.get("Date", "")),
                    "message_id": _normalize_message_ref(msg.get("Message-ID", "")),
                    "in_reply_to": _normalize_message_ref(msg.get("In-Reply-To", "")),
                    "references": " ".join(_extract_reference_chain(msg.get("References", ""))),
                    "attachments": _extract_attachment_metadata(msg),
                }
                if query and query.lower() not in (
                    f"{item['subject']} {item['content']}".lower()
                ):
                    continue
                results.append(item)
            return results
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def _send_sync(
        self,
        mailbox: EmailAccountConfig,
        to: tuple[str, ...],
        subject: str,
        body: str,
        reply_to: str | None,
        cc: tuple[str, ...] = (),
        html_body: str = "",
    ) -> str:
        message = EmailMessage()
        message["From"] = mailbox.address or mailbox.username
        message["To"] = ", ".join(to)
        if cc:
            message["Cc"] = ", ".join(cc)
        message["Subject"] = subject
        if reply_to:
            formatted_reply_to = _format_message_ref(reply_to)
            message["In-Reply-To"] = formatted_reply_to
            message["References"] = formatted_reply_to
        message["Message-ID"] = make_msgid()
        message.set_content(body)
        if html_body:
            message.add_alternative(html_body, subtype="html")

        if mailbox.use_ssl:
            client = smtplib.SMTP_SSL(mailbox.smtp_host, mailbox.smtp_port)
        else:
            client = smtplib.SMTP(mailbox.smtp_host, mailbox.smtp_port)
        try:
            client.login(mailbox.username, mailbox.password)
            client.send_message(message)
        finally:
            try:
                client.quit()
            except Exception:
                pass
        return _normalize_message_ref(str(message["Message-ID"]))

    async def download_attachment(
        self,
        message_id: str,
        content_id: str,
        *,
        account: str = "worker_mailbox",
        folder: str = "INBOX",
    ) -> bytes:
        mailbox = self._get_account(account)
        self._check_folder_permission(folder, account)
        return await asyncio.to_thread(
            self._download_attachment_sync,
            mailbox,
            message_id,
            content_id,
            folder,
        )

    def _list_folders_sync(self, mailbox: EmailAccountConfig) -> list[str]:
        client = imaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port)
        try:
            client.login(mailbox.username, mailbox.password)
            status, data = client.list()
            if status != "OK":
                return []
            folders: list[str] = []
            for row in data:
                parts = row.decode("utf-8", errors="ignore").split(' "/" ')
                folders.append(parts[-1].strip('"'))
            return folders
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def _get_account(self, account: str) -> EmailAccountConfig:
        if account == "worker_mailbox":
            return self._config.worker_mailbox
        if account in {"owner_mailbox", "proxy_send"}:
            return self._config.owner_mailbox
        raise EmailClientError(f"Unknown mailbox account: {account}")

    def _account_for_send_mode(self, send_mode: str) -> EmailAccountConfig:
        if send_mode == "worker_mailbox":
            return self._config.worker_mailbox
        if send_mode in {"owner_mailbox", "proxy_send"}:
            return self._config.owner_mailbox
        raise EmailClientError(f"Unknown send mode: {send_mode}")

    def _download_attachment_sync(
        self,
        mailbox: EmailAccountConfig,
        message_id: str,
        content_id: str,
        folder: str,
    ) -> bytes:
        normalized_message_id = _normalize_message_ref(message_id)
        normalized_content_id = _normalize_message_ref(content_id)
        client = imaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port)
        try:
            client.login(mailbox.username, mailbox.password)
            client.select(folder)
            status, data = client.search(
                None,
                "HEADER",
                "Message-ID",
                _format_message_ref(normalized_message_id),
            )
            if status != "OK" or not data or not data[0]:
                raise EmailClientError(f"Message not found: {message_id}")
            uid = data[0].split()[0]
            fetch_status, raw_parts = client.fetch(uid, "(RFC822)")
            if fetch_status != "OK" or not raw_parts:
                raise EmailClientError(f"Message not found: {message_id}")
            raw_email = _extract_raw_email(raw_parts)
            msg = email.message_from_bytes(raw_email)
            for part in msg.walk():
                part_cid = _normalize_message_ref(part.get("Content-ID", ""))
                part_filename = str(part.get_filename() or "").strip()
                if normalized_content_id in {part_cid, part_filename}:
                    payload = part.get_payload(decode=True)
                    return payload or b""
            raise EmailClientError(f"Attachment not found: {content_id}")
        finally:
            try:
                client.logout()
            except Exception:
                pass


def _extract_email_body(message: email.message.Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            disposition = str(part.get("Content-Disposition", ""))
            if (
                part.get_content_type() == "text/plain"
                and "attachment" not in disposition.lower()
            ):
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
        return ""
    payload = message.get_payload(decode=True)
    if payload is None:
        return str(message.get_payload())
    return payload.decode(message.get_content_charset() or "utf-8", errors="ignore")


def _parse_email_date(raw_date: str) -> str:
    try:
        return parsedate_to_datetime(raw_date).isoformat()
    except Exception:
        return raw_date


def _extract_attachment_metadata(
    message: email.message.Message,
) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    if not message.is_multipart():
        return attachments
    for part in message.walk():
        disposition = str(part.get("Content-Disposition", ""))
        if "attachment" not in disposition.lower():
            continue
        payload = part.get_payload(decode=True)
        attachments.append({
            "filename": str(part.get_filename() or ""),
            "content_type": str(part.get_content_type() or "application/octet-stream"),
            "content_id": (
                _normalize_message_ref(part.get("Content-ID", ""))
                or str(part.get_filename() or "")
            ),
            "size": len(payload) if payload else 0,
        })
    return attachments


def _extract_reference_chain(raw_references: str) -> tuple[str, ...]:
    return tuple(
        normalized
        for normalized in (
            _normalize_message_ref(part)
            for part in str(raw_references or "").split()
        )
        if normalized
    )


def _normalize_message_ref(value: str) -> str:
    return str(value or "").strip().strip("<>").strip()


def _format_message_ref(value: str) -> str:
    normalized = _normalize_message_ref(value)
    return f"<{normalized}>" if normalized else ""


def _extract_raw_email(raw_parts: Any) -> bytes:
    if isinstance(raw_parts, list):
        for part in raw_parts:
            if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], bytes):
                return part[1]
    if isinstance(raw_parts, tuple) and len(raw_parts) > 1 and isinstance(raw_parts[1], bytes):
        return raw_parts[1]
    raise EmailClientError("Unable to decode fetched email payload")
