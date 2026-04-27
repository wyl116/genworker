"""
Internal streaming event definitions - all frozen dataclasses.

Event categories:
- Lifecycle: RunStarted, RunFinished, StepStarted, StepFinished
- Text: TextMessage (complete text output from a round)
- Tool: ToolCall (tool invocation with result)
- Control: PermissionDenial, BudgetExceeded, Error
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Union


class EventType(str, Enum):
    """Internal domain event types for execution streams."""
    RUN_STARTED = "RUN_STARTED"
    RUN_FINISHED = "RUN_FINISHED"
    STEP_STARTED = "STEP_STARTED"
    STEP_FINISHED = "STEP_FINISHED"
    TEXT_MESSAGE = "TEXT_MESSAGE"
    TOOL_CALL = "TOOL_CALL"
    PERMISSION_DENIAL = "PERMISSION_DENIAL"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    ERROR = "ERROR"
    TASK_SPAWNED = "TASK_SPAWNED"
    TASK_PROGRESS = "TASK_PROGRESS"
    QUEUE_STATUS = "QUEUE_STATUS"
    APPROVAL_PENDING = "APPROVAL_PENDING"


def _now() -> str:
    """ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RunStartedEvent:
    """Emitted when an engine execution begins."""
    run_id: str
    thread_id: str = ""
    timestamp: str = field(default_factory=_now)
    event_type: str = field(default=EventType.RUN_STARTED, init=False)


@dataclass(frozen=True)
class RunFinishedEvent:
    """Emitted when an engine execution completes."""
    run_id: str
    success: bool = True
    duration_ms: int = 0
    stop_reason: str = ""
    timestamp: str = field(default_factory=_now)
    event_type: str = field(default=EventType.RUN_FINISHED, init=False)


@dataclass(frozen=True)
class StepStartedEvent:
    """Emitted when a workflow/hybrid step begins."""
    run_id: str
    step_name: str
    step_type: str = ""
    timestamp: str = field(default_factory=_now)
    event_type: str = field(default=EventType.STEP_STARTED, init=False)


@dataclass(frozen=True)
class StepFinishedEvent:
    """Emitted when a workflow/hybrid step completes."""
    run_id: str
    step_name: str
    success: bool = True
    timestamp: str = field(default_factory=_now)
    event_type: str = field(default=EventType.STEP_FINISHED, init=False)


@dataclass(frozen=True)
class TextMessageEvent:
    """Emitted when the LLM produces text output."""
    run_id: str
    content: str
    role: str = "assistant"
    timestamp: str = field(default_factory=_now)
    event_type: str = field(default=EventType.TEXT_MESSAGE, init=False)


@dataclass(frozen=True)
class ToolCallEvent:
    """Emitted when a tool is invoked and returns a result."""
    run_id: str
    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_result: str = ""
    is_error: bool = False
    timestamp: str = field(default_factory=_now)
    event_type: str = field(default=EventType.TOOL_CALL, init=False)


@dataclass(frozen=True)
class PermissionDenialEvent:
    """Emitted when a tool call is denied by the sandbox."""
    run_id: str
    tool_name: str
    reason: str = ""
    timestamp: str = field(default_factory=_now)
    event_type: str = field(default=EventType.PERMISSION_DENIAL, init=False)


@dataclass(frozen=True)
class BudgetExceededEvent:
    """Emitted when token budget is exceeded (NOT an exception)."""
    run_id: str
    max_tokens: int = 0
    used_tokens: int = 0
    stop_reason: str = "budget_exceeded"
    timestamp: str = field(default_factory=_now)
    event_type: str = field(default=EventType.BUDGET_EXCEEDED, init=False)


@dataclass(frozen=True)
class ErrorEvent:
    """Emitted on unrecoverable errors."""
    run_id: str
    code: str = "ENGINE_ERROR"
    message: str = ""
    timestamp: str = field(default_factory=_now)
    event_type: str = field(default=EventType.ERROR, init=False)


@dataclass(frozen=True)
class TaskSpawnedEvent:
    """Emitted when an async task is spawned from a conversation or event."""
    run_id: str
    task_id: str = ""
    task_description: str = ""
    estimated_duration: Optional[str] = None
    timestamp: str = field(default_factory=_now)
    event_type: str = field(default=EventType.TASK_SPAWNED, init=False)


@dataclass(frozen=True)
class TaskProgressEvent:
    """Emitted when an async task reports progress."""
    run_id: str
    task_id: str = ""
    progress: float = 0.0
    current_step: Optional[str] = None
    timestamp: str = field(default_factory=_now)
    event_type: str = field(default=EventType.TASK_PROGRESS, init=False)


@dataclass(frozen=True)
class QueueStatusEvent:
    """Emitted when a queued service request changes status."""
    run_id: str
    thread_id: str
    tenant_id: str
    worker_id: str
    status: str
    position: int = 0
    queue_size: int = 0
    timestamp: str = field(default_factory=_now)
    event_type: str = field(default=EventType.QUEUE_STATUS, init=False)


@dataclass(frozen=True)
class ApprovalPendingEvent:
    """Emitted when execution pauses for human approval."""
    run_id: str
    thread_id: str
    inbox_id: str
    prompt: str
    timestamp: str = field(default_factory=_now)
    event_type: str = field(default=EventType.APPROVAL_PENDING, init=False)


StreamEvent = Union[
    RunStartedEvent,
    RunFinishedEvent,
    StepStartedEvent,
    StepFinishedEvent,
    TextMessageEvent,
    ToolCallEvent,
    PermissionDenialEvent,
    BudgetExceededEvent,
    ErrorEvent,
    TaskSpawnedEvent,
    TaskProgressEvent,
    QueueStatusEvent,
    ApprovalPendingEvent,
]
