"""
AG-UI SSE formatter.

The internal StreamEvent model stays domain-oriented. This formatter is the
only place that knows how to expand those events into AG-UI wire events.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import uuid4

from src.streaming.events import (
    ApprovalPendingEvent,
    BudgetExceededEvent,
    ErrorEvent,
    PermissionDenialEvent,
    QueueStatusEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    StreamEvent,
    TaskProgressEvent,
    TaskSpawnedEvent,
    TextMessageEvent,
    ToolCallEvent,
)

from .base import BaseSseFormatter


def _timestamp_to_ms(timestamp: str) -> int | None:
    """Convert ISO-8601 timestamps from internal events into epoch millis."""
    if not timestamp:
        return None
    normalized = timestamp.replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(normalized).timestamp() * 1000)
    except ValueError:
        return None


class AgUiSseFormatter(BaseSseFormatter):
    """Expand internal domain events into AG-UI-compatible SSE payloads."""

    def __init__(self, *, thread_id: str | None = None) -> None:
        self._thread_id = thread_id or ""
        self._errored_runs: set[str] = set()

    def serialize(self, event: StreamEvent) -> list[dict[str, Any]]:
        if isinstance(event, RunStartedEvent):
            return [self._serialize_run_started(event)]
        if isinstance(event, RunFinishedEvent):
            return self._serialize_run_finished(event)
        if isinstance(event, StepStartedEvent):
            return [self._serialize_step_started(event)]
        if isinstance(event, StepFinishedEvent):
            return [self._serialize_step_finished(event)]
        if isinstance(event, TextMessageEvent):
            return self._serialize_text_message(event)
        if isinstance(event, ToolCallEvent):
            return self._serialize_tool_call(event)
        if isinstance(event, ErrorEvent):
            return [self._serialize_run_error(event)]
        if isinstance(event, PermissionDenialEvent):
            return [self._serialize_custom("permission_denial", event)]
        if isinstance(event, BudgetExceededEvent):
            return [self._serialize_custom("budget_exceeded", event)]
        if isinstance(event, TaskSpawnedEvent):
            return [self._serialize_custom("task_spawned", event)]
        if isinstance(event, TaskProgressEvent):
            return [self._serialize_custom("task_progress", event)]
        if isinstance(event, QueueStatusEvent):
            return [self._serialize_custom("queue_status", event)]
        if isinstance(event, ApprovalPendingEvent):
            return [self._serialize_custom("approval_pending", event)]
        return [{
            "type": "CUSTOM",
            "name": "stream_event",
            "value": asdict(event),
        }]

    def _serialize_run_started(self, event: RunStartedEvent) -> dict[str, Any]:
        thread_id = self._resolve_thread_id(event.run_id, event.thread_id)
        return {
            "type": "RUN_STARTED",
            "threadId": thread_id,
            "runId": event.run_id,
            "timestamp": _timestamp_to_ms(event.timestamp),
        }

    def _serialize_run_finished(self, event: RunFinishedEvent) -> list[dict[str, Any]]:
        if not event.success:
            if event.run_id in self._errored_runs:
                return []
            return [{
                "type": "RUN_ERROR",
                "message": event.stop_reason or "Run finished unsuccessfully",
                "code": "RUN_FAILED",
                "runId": event.run_id,
                "timestamp": _timestamp_to_ms(event.timestamp),
            }]
        return [{
            "type": "RUN_FINISHED",
            "threadId": self._resolve_thread_id(event.run_id),
            "runId": event.run_id,
            "timestamp": _timestamp_to_ms(event.timestamp),
        }]

    def _serialize_step_started(self, event: StepStartedEvent) -> dict[str, Any]:
        return {
            "type": "STEP_STARTED",
            "stepName": event.step_name,
            "timestamp": _timestamp_to_ms(event.timestamp),
            "metadata": {"stepType": event.step_type} if event.step_type else None,
        }

    def _serialize_step_finished(self, event: StepFinishedEvent) -> dict[str, Any]:
        return {
            "type": "STEP_FINISHED",
            "stepName": event.step_name,
            "timestamp": _timestamp_to_ms(event.timestamp),
            "metadata": {"success": event.success},
        }

    def _serialize_text_message(self, event: TextMessageEvent) -> list[dict[str, Any]]:
        message_id = uuid4().hex
        timestamp = _timestamp_to_ms(event.timestamp)
        return [
            {
                "type": "TEXT_MESSAGE_START",
                "messageId": message_id,
                "role": event.role,
                "timestamp": timestamp,
            },
            {
                "type": "TEXT_MESSAGE_CONTENT",
                "messageId": message_id,
                "delta": event.content,
                "timestamp": timestamp,
            },
            {
                "type": "TEXT_MESSAGE_END",
                "messageId": message_id,
                "timestamp": timestamp,
            },
        ]

    def _serialize_tool_call(self, event: ToolCallEvent) -> list[dict[str, Any]]:
        tool_call_id = uuid4().hex
        message_id = uuid4().hex
        timestamp = _timestamp_to_ms(event.timestamp)
        payloads = [
            {
                "type": "TOOL_CALL_START",
                "toolCallId": tool_call_id,
                "toolCallName": event.tool_name,
                "timestamp": timestamp,
            },
        ]
        tool_args = json.dumps(event.tool_input, ensure_ascii=False)
        if tool_args and tool_args != "{}":
            payloads.append({
                "type": "TOOL_CALL_ARGS",
                "toolCallId": tool_call_id,
                "delta": tool_args,
                "timestamp": timestamp,
            })
        payloads.extend([
            {
                "type": "TOOL_CALL_END",
                "toolCallId": tool_call_id,
                "timestamp": timestamp,
            },
            {
                "type": "TOOL_CALL_RESULT",
                "messageId": message_id,
                "toolCallId": tool_call_id,
                "content": event.tool_result,
                "role": "tool",
                "timestamp": timestamp,
            },
        ])
        return payloads

    def _serialize_run_error(self, event: ErrorEvent) -> dict[str, Any]:
        if event.run_id:
            self._errored_runs.add(event.run_id)
        return {
            "type": "RUN_ERROR",
            "message": event.message,
            "code": event.code,
            "runId": event.run_id or None,
            "timestamp": _timestamp_to_ms(event.timestamp),
        }

    def _serialize_custom(self, name: str, event: Any) -> dict[str, Any]:
        if name == "permission_denial":
            value = {
                "runId": event.run_id,
                "toolName": event.tool_name,
                "reason": event.reason,
                "timestamp": _timestamp_to_ms(event.timestamp),
            }
        elif name == "budget_exceeded":
            value = {
                "runId": event.run_id,
                "maxTokens": event.max_tokens,
                "usedTokens": event.used_tokens,
                "stopReason": event.stop_reason,
                "timestamp": _timestamp_to_ms(event.timestamp),
            }
        elif name == "task_spawned":
            value = {
                "runId": event.run_id,
                "taskId": event.task_id,
                "taskDescription": event.task_description,
                "estimatedDuration": event.estimated_duration,
                "timestamp": _timestamp_to_ms(event.timestamp),
            }
        elif name == "task_progress":
            value = {
                "runId": event.run_id,
                "taskId": event.task_id,
                "progress": event.progress,
                "currentStep": event.current_step,
                "timestamp": _timestamp_to_ms(event.timestamp),
            }
        elif name == "queue_status":
            self._thread_id = event.thread_id or self._thread_id
            value = {
                "runId": event.run_id,
                "threadId": event.thread_id,
                "tenantId": event.tenant_id,
                "workerId": event.worker_id,
                "status": event.status,
                "position": event.position,
                "queueSize": event.queue_size,
                "timestamp": _timestamp_to_ms(event.timestamp),
            }
        elif name == "approval_pending":
            self._thread_id = event.thread_id or self._thread_id
            value = {
                "runId": event.run_id,
                "threadId": event.thread_id,
                "inboxId": event.inbox_id,
                "prompt": event.prompt,
                "timestamp": _timestamp_to_ms(event.timestamp),
            }
        else:
            value = asdict(event)
        return {
            "type": "CUSTOM",
            "name": name,
            "value": value,
        }

    def _resolve_thread_id(self, run_id: str, event_thread_id: str = "") -> str:
        thread_id = event_thread_id or self._thread_id or f"run:{run_id}"
        self._thread_id = thread_id
        return thread_id
