"""Immutable models shared by IM channel adapters and router."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def freeze_data(value: Any) -> Any:
    """Recursively convert mutable containers into tuple-based structures."""
    if isinstance(value, dict):
        return tuple((str(key), freeze_data(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return tuple(freeze_data(item) for item in value)
    return value


def thaw_data(value: Any) -> Any:
    """Recursively convert tuple-based frozen data back into mutable containers."""
    if isinstance(value, tuple):
        if all(isinstance(item, tuple) and len(item) == 2 for item in value):
            return {str(key): thaw_data(item) for key, item in value}
        return [thaw_data(item) for item in value]
    return value


@dataclass(frozen=True)
class Mention:
    """One @ mention parsed from a platform message."""

    user_id: str
    name: str = ""
    is_bot: bool = False


@dataclass(frozen=True)
class Attachment:
    """One channel attachment descriptor."""

    file_key: str
    file_name: str = ""
    file_type: str = "file"
    size: int = 0


@dataclass(frozen=True)
class ChannelInboundMessage:
    """Channel-neutral inbound IM message."""

    message_id: str
    channel_type: str
    chat_id: str
    chat_type: str = "group"
    sender_id: str = ""
    sender_name: str = ""
    content: str = ""
    msg_type: str = "text"
    reply_to_id: str | None = None
    mentions: tuple[Mention, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    raw_event: tuple[tuple[str, Any], ...] = ()
    timestamp: str = field(default_factory=_utc_now)
    metadata: tuple[tuple[str, Any], ...] = ()

    @property
    def metadata_dict(self) -> dict[str, Any]:
        return thaw_data(self.metadata)


@dataclass(frozen=True)
class ReplyContent:
    """Unified outbound reply content."""

    content_type: str = "text"
    text: str = ""
    card: tuple[tuple[str, Any], ...] = ()
    file_key: str = ""
    metadata: tuple[tuple[str, Any], ...] = ()

    @property
    def card_dict(self) -> dict[str, Any]:
        card = thaw_data(self.card)
        return card if isinstance(card, dict) else {}


@dataclass(frozen=True)
class StreamChunk:
    """One outbound stream chunk."""

    chunk_type: str = "text_delta"
    content: str = ""
    metadata: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class ChannelBinding:
    """One channel-to-worker binding loaded from PERSONA.md."""

    channel_type: str
    connection_mode: str
    chat_ids: tuple[str, ...]
    tenant_id: str
    worker_id: str
    reply_mode: str = "complete"
    features: tuple[tuple[str, Any], ...] = ()

    @property
    def features_dict(self) -> dict[str, Any]:
        features = thaw_data(self.features)
        return features if isinstance(features, dict) else {}

    @property
    def adapter_id(self) -> str:
        return f"{self.channel_type}:{self.tenant_id}:{self.worker_id}"


def build_channel_binding(
    raw: dict[str, Any],
    *,
    tenant_id: str,
    worker_id: str,
) -> ChannelBinding:
    """Parse one raw worker channel mapping into a frozen binding."""
    channel_type = str(raw.get("type", raw.get("channel_type", ""))).strip().lower()
    connection_mode = str(raw.get("connection_mode", "webhook")).strip().lower()
    chat_ids = tuple(str(item).strip() for item in raw.get("chat_ids", ()) if str(item).strip())
    reply_mode = str(raw.get("reply_mode", "complete")).strip().lower() or "complete"
    features = raw.get("features", {})
    return ChannelBinding(
        channel_type=channel_type,
        connection_mode=connection_mode,
        chat_ids=chat_ids,
        tenant_id=tenant_id,
        worker_id=worker_id,
        reply_mode=reply_mode,
        features=freeze_data(features),
    )
