"""Protocols for bidirectional IM channel adapters."""
from __future__ import annotations

from typing import Any, AsyncGenerator, Awaitable, Callable, Protocol, runtime_checkable

from .models import ChannelInboundMessage, ReplyContent, StreamChunk

MessageCallback = Callable[[ChannelInboundMessage], Awaitable[None]]


@runtime_checkable
class IMChannelAdapter(Protocol):
    """Bidirectional IM channel adapter contract."""

    @property
    def channel_type(self) -> str:
        ...

    def supports_streaming(self) -> bool:
        ...

    async def start(self, message_callback: MessageCallback) -> None:
        ...

    async def stop(self) -> None:
        ...

    async def health_check(self) -> bool:
        ...

    async def parse_event(self, raw_event: Any) -> ChannelInboundMessage | None:
        ...

    async def reply(
        self,
        source_msg: ChannelInboundMessage,
        content: ReplyContent,
    ) -> str:
        ...

    async def reply_stream(
        self,
        source_msg: ChannelInboundMessage,
        chunks: AsyncGenerator[StreamChunk, None],
    ) -> str:
        ...

    async def send_message(
        self,
        chat_id: str,
        content: ReplyContent,
    ) -> str:
        ...

    async def handle_webhook(self, request: Any) -> Any:
        ...
