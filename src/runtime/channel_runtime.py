"""Runtime helpers extracted from the channel message router."""
from __future__ import annotations

from typing import Any, AsyncGenerator

from src.conversation.models import ChatMessage
from src.streaming.events import EventType

from src.channels.models import ReplyContent, StreamChunk, freeze_data
from src.worker.sensing.protocol import SensedFact


class StreamCollector:
    """Collect streamed reply text and spawned task references."""

    __slots__ = (
        "parts",
        "spawned_task_ids",
        "spawned_task_descriptions",
        "reply_content",
    )

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.spawned_task_ids: list[str] = []
        self.spawned_task_descriptions: list[str] = []
        self.reply_content = ReplyContent(text="")

    def finalize(self) -> None:
        text = "\n\n".join(
            part.strip() for part in self.parts if str(part).strip()
        )
        self.reply_content = ReplyContent(text=text)


def build_thread_id(message, binding) -> str:
    features = binding.features_dict
    session_mode = str(features.get("session_mode", "per_user")).strip().lower()
    if message.chat_type == "p2p":
        if message.reply_to_id:
            return f"im:{message.channel_type}:{message.chat_id}:thread:{message.reply_to_id}"
        return f"im:{message.channel_type}:{message.chat_id}:{message.sender_id or 'anonymous'}"
    if session_mode == "per_chat":
        return f"im:{message.channel_type}:{message.chat_id}"
    if session_mode == "per_thread" and message.reply_to_id:
        return f"im:{message.channel_type}:{message.chat_id}:thread:{message.reply_to_id}"
    if message.reply_to_id and session_mode == "per_user":
        return f"im:{message.channel_type}:{message.chat_id}:thread:{message.reply_to_id}"
    return f"im:{message.channel_type}:{message.chat_id}:{message.sender_id or 'anonymous'}"


async def collect_reply(
    event_stream: AsyncGenerator[Any, None],
) -> tuple[ReplyContent, list[str]]:
    parts: list[str] = []
    spawned_task_ids: list[str] = []
    spawned_task_descriptions: list[str] = []
    async for event in event_stream:
        event_type = getattr(event, "event_type", "")
        if event_type == EventType.TEXT_MESSAGE:
            text = getattr(event, "content", "")
            if text:
                parts.append(text)
        elif event_type == EventType.TASK_SPAWNED:
            task_id = getattr(event, "task_id", "")
            if task_id:
                spawned_task_ids.append(task_id)
            description = getattr(event, "task_description", "") or task_id
            if description:
                spawned_task_descriptions.append(description)
        elif event_type == EventType.ERROR:
            parts.append(getattr(event, "message", "处理失败"))

    if spawned_task_descriptions:
        parts.extend(f"已创建后台任务: {desc}" for desc in spawned_task_descriptions)
    text = "\n\n".join(part.strip() for part in parts if str(part).strip())
    return ReplyContent(text=text), spawned_task_ids


async def prepare_stream(
    event_stream: AsyncGenerator[Any, None],
) -> tuple[AsyncGenerator[StreamChunk, None], StreamCollector]:
    collector = StreamCollector()

    async def _generate() -> AsyncGenerator[StreamChunk, None]:
        async for event in event_stream:
            event_type = getattr(event, "event_type", "")
            if event_type == EventType.TEXT_MESSAGE:
                text = getattr(event, "content", "")
                if text:
                    collector.parts.append(text)
                    yield StreamChunk(chunk_type="text_delta", content=text)
            elif event_type == EventType.TASK_SPAWNED:
                task_id = getattr(event, "task_id", "")
                if task_id:
                    collector.spawned_task_ids.append(task_id)
                description = getattr(event, "task_description", "") or task_id
                if description:
                    collector.spawned_task_descriptions.append(description)
            elif event_type == EventType.ERROR:
                error_text = getattr(event, "message", "处理失败")
                collector.parts.append(error_text)
                yield StreamChunk(chunk_type="text_delta", content=error_text)

        for desc in collector.spawned_task_descriptions:
            suffix = f"\n\n已创建后台任务: {desc}"
            collector.parts.append(suffix)
            yield StreamChunk(chunk_type="text_delta", content=suffix)
        yield StreamChunk(chunk_type="finished")

    async def _wrapped() -> AsyncGenerator[StreamChunk, None]:
        try:
            async for chunk in _generate():
                yield chunk
        finally:
            collector.finalize()

    return _wrapped(), collector


def should_route_to_sensor(binding, message) -> bool:
    features = binding.features_dict
    if message.msg_type in {"file", "image"}:
        return bool(features.get("monitor_file_share"))
    if message.msg_type == "system":
        return bool(features.get("monitor_system_events"))
    return bool(features.get("monitor_group_chat"))


def build_sensed_fact(message) -> SensedFact:
    return SensedFact(
        source_type=message.channel_type,
        event_type=f"data.im.{message.msg_type}",
        dedupe_key=message.message_id,
        payload=(
            ("chat_id", message.chat_id),
            ("sender_id", message.sender_id),
            ("sender_name", message.sender_name),
            ("content", message.content),
            ("msg_type", message.msg_type),
        ),
        priority_hint=20,
        cognition_route="heartbeat",
    )


def extract_chat_id(thread_id: str) -> str:
    parts = thread_id.split(":", 3)
    if len(parts) < 3:
        return ""
    return parts[2]


def build_task_context(message) -> str:
    parts = [f"channel_type:{message.channel_type}"]
    metadata = message.metadata_dict
    subject = str(metadata.get("subject", "")).strip()
    if subject:
        parts.append(f"subject:{subject}")
    return ", ".join(parts)


async def load_session(session_manager, thread_id: str) -> Any | None:
    finder = getattr(session_manager, "find_by_thread", None)
    if callable(finder):
        try:
            return await finder(thread_id)
        except Exception:
            return None
    cache = getattr(session_manager, "_cache", None)
    if isinstance(cache, dict):
        return cache.get(thread_id)
    return None


def build_task_completed_reply(description: str, session) -> ReplyContent:
    reply_metadata = ()
    if session is not None:
        session_meta = dict(getattr(session, "metadata", ()))
        recipient = str(session_meta.get("sender_id", "")).strip()
        subject = str(session_meta.get("subject", "")).strip()
        if recipient or subject:
            reply_metadata = freeze_data({
                "recipient": recipient,
                "subject": subject,
            })
    return ReplyContent(
        text=f"任务已完成: {description}",
        metadata=reply_metadata,
    )


def build_task_failed_reply(description: str, error_message: str, session) -> ReplyContent:
    reply_metadata = ()
    if session is not None:
        session_meta = dict(getattr(session, "metadata", ()))
        recipient = str(session_meta.get("sender_id", "")).strip()
        subject = str(session_meta.get("subject", "")).strip()
        if recipient or subject:
            reply_metadata = freeze_data({
                "recipient": recipient,
                "subject": subject,
            })
    detail = error_message.strip() or "unknown error"
    label = description.strip() or "后台任务"
    return ReplyContent(
        text=f"任务执行失败: {label}\n原因: {detail}",
        metadata=reply_metadata,
    )


def append_reply_to_session(session, reply_content: ReplyContent, spawned_task_ids: list[str]):
    if reply_content.text:
        session = session.append_message(ChatMessage(role="assistant", content=reply_content.text))
    for task_id in spawned_task_ids:
        session = session.add_spawned_task(task_id)
    return session
