"""Shared outbound channel models."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SenderScope:
    """Resolved sender identity for worker-scoped outbound operations."""

    tenant_id: str
    worker_id: str


@dataclass(frozen=True)
class ChannelMessage:
    """Message to send through an outbound channel adapter."""

    channel: str
    recipients: tuple[str, ...]
    subject: str
    content: str
    message_type: str = "progress_update"
    priority: str = "normal"
    reply_to: str | None = None
    attachments: tuple[str, ...] = ()
    im_chat_id: str | None = None
    sender_tenant_id: str = ""
    sender_worker_id: str = ""


@dataclass(frozen=True)
class RetryConfig:
    """Exponential backoff retry configuration."""

    max_retries: int = 3
    backoff_base: float = 2.0
    backoff_max: float = 60.0


@dataclass(frozen=True)
class ChannelPriority:
    """Outbound channel priority entry used by fallback senders."""

    channel_type: str
    adapter: Any
