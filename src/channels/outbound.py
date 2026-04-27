"""Unified outbound channel adapters and retry/fallback helpers."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Protocol, runtime_checkable

from src.channels.models import ReplyContent

from .outbound_types import ChannelMessage, ChannelPriority, RetryConfig, SenderScope

logger = logging.getLogger(__name__)


class ChannelSendError(Exception):
    """Raised when all channel send attempts have been exhausted."""

    def __init__(self, message: str, attempts: int = 0) -> None:
        super().__init__(message)
        self.attempts = attempts


class EventPublisher(Protocol):
    """Protocol for publishing integration events."""

    async def publish(self, event: Any) -> int: ...


@runtime_checkable
class ChannelAdapter(Protocol):
    """Abstract outbound channel interface."""

    async def send(self, message: ChannelMessage) -> str: ...

    async def update_document(
        self,
        path: str,
        content: str,
        section: str | None = None,
        *,
        scope: SenderScope,
    ) -> bool: ...


class EmailChannelAdapter:
    """Email channel backed by the MCP email tool."""

    def __init__(self, tool_executor: Any, im_adapter: Any | None = None) -> None:
        self._tool_executor = tool_executor
        self._im_adapter = im_adapter

    async def send(self, message: ChannelMessage) -> str:
        if self._im_adapter is not None and message.im_chat_id:
            return await self._im_adapter.send_message(
                message.im_chat_id,
                ReplyContent(text=message.content),
            )
        recipients = ", ".join(message.recipients)
        tool_input = {
            "to": recipients,
            "subject": message.subject,
            "body": message.content,
        }
        if message.reply_to:
            tool_input["reply_to"] = message.reply_to

        await self._tool_executor.execute("email_send", tool_input)
        return f"email-{uuid.uuid4().hex[:8]}"

    async def update_document(
        self,
        path: str,
        content: str,
        section: str | None = None,
        *,
        scope: SenderScope | None = None,
    ) -> bool:
        return False


class DirectEmailAdapter:
    """Email channel backed by a worker-scoped EmailClient."""

    def __init__(self, email_client: Any, im_adapter: Any | None = None) -> None:
        self._email_client = email_client
        self._im_adapter = im_adapter

    async def send(self, message: ChannelMessage) -> str:
        if self._im_adapter is not None and message.im_chat_id:
            return await self._im_adapter.send_message(
                message.im_chat_id,
                ReplyContent(text=message.content),
            )
        return await self._email_client.send(
            to=message.recipients,
            subject=message.subject,
            body=message.content,
            send_mode="worker_mailbox",
            reply_to=message.reply_to,
        )

    async def update_document(
        self,
        path: str,
        content: str,
        section: str | None = None,
        *,
        scope: SenderScope | None = None,
    ) -> bool:
        return False


class FeishuChannelAdapter:
    """Feishu channel for outbound messages and document updates."""

    def __init__(
        self,
        mount_manager: Any,
        feishu_bot: Any | None = None,
        im_adapter: Any | None = None,
    ) -> None:
        self._mount_manager = mount_manager
        self._feishu_bot = feishu_bot
        self._im_adapter = im_adapter

    async def send(self, message: ChannelMessage) -> str:
        if self._im_adapter is not None and message.im_chat_id:
            return await self._im_adapter.send_message(
                message.im_chat_id,
                ReplyContent(text=message.content),
            )
        msg_id = f"feishu-{uuid.uuid4().hex[:8]}"
        if self._feishu_bot is not None:
            await self._feishu_bot.send_message(
                recipients=message.recipients,
                content=message.content,
            )
        logger.debug(
            "[FeishuAdapter] Sent message %s to %s",
            msg_id,
            message.recipients,
        )
        return msg_id

    async def update_document(
        self,
        path: str,
        content: str,
        section: str | None = None,
        *,
        scope: SenderScope | None = None,
    ) -> bool:
        try:
            if section is not None:
                existing = await self._mount_manager.read_file(path)
                updated = _replace_section(existing, section, content)
                await self._mount_manager.write_file(path, updated)
            else:
                await self._mount_manager.write_file(path, content)
            logger.debug(
                "[FeishuAdapter] Updated document %s%s",
                path,
                f" section={section}" if section else "",
            )
            return True
        except Exception as exc:
            logger.error("[FeishuAdapter] Document update failed: %s", exc)
            return False


class WeComChannelAdapter:
    """WeCom channel backed by a worker-scoped client."""

    def __init__(self, wecom_client: Any, im_adapter: Any | None = None) -> None:
        self._wecom_client = wecom_client
        self._im_adapter = im_adapter

    async def send(self, message: ChannelMessage) -> str:
        if self._im_adapter is not None and message.im_chat_id:
            return await self._im_adapter.send_message(
                message.im_chat_id,
                ReplyContent(text=message.content),
            )
        result = await self._wecom_client.send_message(
            recipients=message.recipients,
            content=message.content,
        )
        return str(result.get("msgid", "wecom-msg"))

    async def update_document(
        self,
        path: str,
        content: str,
        section: str | None = None,
        *,
        scope: SenderScope | None = None,
    ) -> bool:
        return False


class DingTalkChannelAdapter:
    """DingTalk channel backed by a worker-scoped client."""

    def __init__(self, dingtalk_client: Any, im_adapter: Any | None = None) -> None:
        self._dingtalk_client = dingtalk_client
        self._im_adapter = im_adapter

    async def send(self, message: ChannelMessage) -> str:
        if self._im_adapter is not None and message.im_chat_id:
            return await self._im_adapter.send_message(
                message.im_chat_id,
                ReplyContent(text=message.content),
            )
        result = await self._dingtalk_client.send_message(
            recipients=message.recipients,
            content=message.content,
        )
        return str(result.get("processQueryKey", "dingtalk-msg"))

    async def update_document(
        self,
        path: str,
        content: str,
        section: str | None = None,
        *,
        scope: SenderScope | None = None,
    ) -> bool:
        return False


class SlackChannelAdapter:
    """Slack channel backed by a worker-scoped client."""

    def __init__(self, slack_client: Any, im_adapter: Any | None = None) -> None:
        self._slack_client = slack_client
        self._im_adapter = im_adapter

    async def send(self, message: ChannelMessage) -> str:
        if self._im_adapter is not None and message.im_chat_id:
            return await self._im_adapter.send_message(
                message.im_chat_id,
                ReplyContent(text=message.content),
            )
        channel = message.recipients[0] if message.recipients else ""
        response = await self._slack_client.post_message(channel, message.content)
        return str(response.get("ts", "slack-msg"))

    async def update_document(
        self,
        path: str,
        content: str,
        section: str | None = None,
        *,
        scope: SenderScope | None = None,
    ) -> bool:
        return False


class ReliableChannelAdapter:
    """Decorator that adds exponential backoff retry to any adapter."""

    def __init__(
        self,
        inner: ChannelAdapter,
        retry_config: RetryConfig,
        event_bus: EventPublisher | None = None,
        tenant_id: str = "demo",
    ) -> None:
        self._inner = inner
        self._retry_config = retry_config
        self._event_bus = event_bus
        self._tenant_id = tenant_id
        self._last_delays: list[float] = []

    async def send(self, message: ChannelMessage) -> str:
        return await self._retry_operation(
            lambda: self._inner.send(message),
            operation_desc="send",
            message=message,
        )

    async def update_document(
        self,
        path: str,
        content: str,
        section: str | None = None,
        *,
        scope: SenderScope | None = None,
    ) -> bool:
        return await self._retry_operation(
            lambda: _call_update_document(
                self._inner,
                path,
                content,
                section,
                scope=scope,
            ),
            operation_desc="update_document",
        )

    async def _retry_operation(
        self,
        operation,
        operation_desc: str,
        message: ChannelMessage | None = None,
    ) -> Any:
        last_error: Exception | None = None
        self._last_delays = []
        max_retries = self._retry_config.max_retries

        for attempt in range(max_retries + 1):
            try:
                return await operation()
            except Exception as exc:
                last_error = exc
                if attempt < max_retries:
                    delay = _compute_backoff(
                        attempt,
                        self._retry_config.backoff_base,
                        self._retry_config.backoff_max,
                    )
                    self._last_delays.append(delay)
                    logger.warning(
                        "[ReliableAdapter] %s attempt %s failed: %s, retrying in %.1fs",
                        operation_desc,
                        attempt + 1,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)

        await self._publish_send_failed(message, last_error, max_retries + 1)
        raise ChannelSendError(
            f"All {max_retries + 1} attempts failed: {last_error}",
            attempts=max_retries + 1,
        )

    async def _publish_send_failed(
        self,
        message: ChannelMessage | None,
        error: Exception | None,
        attempts: int,
    ) -> None:
        if self._event_bus is None:
            return

        from src.events.models import Event

        recipient = (
            ", ".join(message.recipients)
            if message and message.recipients
            else "unknown"
        )
        channel_type = message.channel if message else "unknown"
        await self._event_bus.publish(Event(
            event_id=f"evt-{uuid.uuid4().hex[:8]}",
            type="channel.send_failed",
            source="reliable_channel_adapter",
            tenant_id=self._tenant_id,
            payload=(
                ("channel_type", channel_type),
                ("recipient", recipient),
                ("error", str(error)),
                ("attempts", attempts),
            ),
        ))


class MultiChannelFallback:
    """Fallback sender that tries multiple outbound channels."""

    def __init__(
        self,
        channels: tuple[ChannelPriority, ...],
        event_bus: EventPublisher | None = None,
        tenant_id: str = "demo",
    ) -> None:
        self._channels = channels
        self._event_bus = event_bus
        self._tenant_id = tenant_id

    async def send(self, message: ChannelMessage) -> str:
        if message.priority == "high":
            return await self._broadcast_send(message)
        return await self._fallback_send(message)

    async def update_document(
        self,
        path: str,
        content: str,
        section: str | None = None,
        *,
        scope: SenderScope | None = None,
    ) -> bool:
        for channel in self._channels:
            try:
                result = await _call_update_document(
                    channel.adapter,
                    path,
                    content,
                    section,
                    scope=scope,
                )
                if result:
                    return True
            except Exception as exc:
                logger.warning(
                    "[MultiChannel] update_document via %s failed: %s",
                    channel.channel_type,
                    exc,
                )
        return False

    async def _fallback_send(self, message: ChannelMessage) -> str:
        last_error: Exception | None = None
        for channel in self._channels:
            try:
                return await channel.adapter.send(message)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "[MultiChannel] %s failed: %s, trying next channel",
                    channel.channel_type,
                    exc,
                )

        await self._publish_all_failed(message, last_error)
        raise ChannelSendError(
            f"All channels failed: {last_error}",
            attempts=len(self._channels),
        )

    async def _broadcast_send(self, message: ChannelMessage) -> str:
        results = await asyncio.gather(
            *(channel.adapter.send(message) for channel in self._channels),
            return_exceptions=True,
        )
        successes: list[str] = []
        errors: list[Exception] = []
        for result in results:
            if isinstance(result, Exception):
                errors.append(result)
            else:
                successes.append(result)
        if successes:
            return successes[0]

        last_error = errors[-1] if errors else None
        await self._publish_all_failed(message, last_error)
        raise ChannelSendError(
            f"All broadcast channels failed: {last_error}",
            attempts=len(self._channels),
        )

    async def _publish_all_failed(
        self,
        message: ChannelMessage,
        error: Exception | None,
    ) -> None:
        if self._event_bus is None:
            return

        from src.events.models import Event

        await self._event_bus.publish(Event(
            event_id=f"evt-{uuid.uuid4().hex[:8]}",
            type="channel.all_channels_failed",
            source="multi_channel_fallback",
            tenant_id=self._tenant_id,
            payload=(
                ("recipient", ", ".join(message.recipients)),
                ("error", str(error)),
                ("channels_tried", len(self._channels)),
            ),
        ))


def _compute_backoff(attempt: int, base: float, max_delay: float) -> float:
    delay = base * (2 ** attempt)
    return min(delay, max_delay)


def _replace_section(document: str, section_header: str, new_content: str) -> str:
    lines = document.split("\n")
    result: list[str] = []
    in_section = False
    section_level = 0
    replaced = False

    for line in lines:
        stripped = line.strip()
        if stripped == section_header.strip():
            result.append(line)
            result.append(new_content)
            in_section = True
            section_level = _heading_level(stripped)
            replaced = True
            continue

        if in_section:
            current_level = _heading_level(stripped)
            if current_level > 0 and current_level <= section_level:
                in_section = False
                result.append(line)
            continue

        result.append(line)

    if not replaced:
        result.append("")
        result.append(section_header)
        result.append(new_content)

    return "\n".join(result)


async def _call_update_document(
    adapter: ChannelAdapter | Any,
    path: str,
    content: str,
    section: str | None,
    *,
    scope: SenderScope | None,
) -> bool:
    if scope is None:
        return await adapter.update_document(path, content, section)
    return await adapter.update_document(path, content, section, scope=scope)


def _heading_level(line: str) -> int:
    stripped = line.lstrip()
    if stripped.startswith("#"):
        count = 0
        for ch in stripped:
            if ch == "#":
                count += 1
            else:
                break
        if count <= 6 and (len(stripped) == count or stripped[count] == " "):
            return count
    return 0
