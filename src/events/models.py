"""
Event data models - all frozen dataclasses for immutability.

Defines:
- Event: core event structure with tenant isolation
- EventBus structural protocol and subscription descriptor
- Structured payload types for specific event categories
"""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable


@dataclass(frozen=True)
class Event:
    """
    Core event structure for EventBus.

    Uses tuple-of-tuples for payload and metadata to ensure immutability.
    """
    event_id: str
    type: str              # e.g. "data.file_uploaded"
    source: str
    tenant_id: str
    payload: tuple[tuple[str, Any], ...] = ()
    timestamp: str = ""    # ISO 8601
    metadata: tuple[tuple[str, Any], ...] = ()


EventHandler = Callable[[Event], Awaitable[None]]


@dataclass(frozen=True)
class Subscription:
    """Descriptor for an event handler registration."""

    handler_id: str
    event_type: str
    tenant_id: str
    handler: EventHandler
    filter: tuple[tuple[str, str], ...] = ()


@runtime_checkable
class EventBusProtocol(Protocol):
    """Structured EventBus protocol used by consumers."""

    def subscribe(self, subscription: Subscription) -> str:
        ...

    def unsubscribe(self, tenant_id: str, handler_id: str) -> bool:
        ...

    async def publish(self, event: Event) -> int:
        ...


@dataclass(frozen=True)
class GoalCreatedPayload:
    """Payload for goal.created_from_external events."""
    goal_id: str
    source_type: str       # "email" | "feishu" | "dingtalk" | "webhook"
    source_id: str
    title: str
    require_approval: bool
    confidence: float


@dataclass(frozen=True)
class ChannelFailedPayload:
    """Payload for channel.send_failed / channel.all_channels_failed events."""
    channel_type: str      # "email" | "feishu" | "dingtalk"
    recipient: str
    error: str
    attempts: int
    fallback_attempted: bool = False


@dataclass(frozen=True)
class MountAuthExpiredPayload:
    """Payload for mount.auth_expired events."""
    mount_id: str
    platform: str          # "feishu" | "wecom" | "dingtalk"
    error: str
    refresh_attempted: bool


@dataclass(frozen=True)
class SubAgentEventPayload:
    """Payload for subagent lifecycle events."""
    subagent_id: str
    parent_run_id: str
    sub_goal_id: str
    status: str            # "started" | "progress" | "completed" | "failed" | "timeout"
    progress: float = 0.0
    error: str = ""


@dataclass(frozen=True)
class TaskFailedPayload:
    """Payload for task.failed events (spawn_task failures)."""
    task_id: str
    session_id: str
    error_code: str
    error_message: str
