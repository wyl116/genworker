"""
Legacy SSE formatter.

This preserves the project's original wire format for backward compatibility.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from src.streaming.events import EventType, StreamEvent

from .base import BaseSseFormatter


_LEGACY_TYPE_MAP: dict[str, str] = {
    EventType.RUN_STARTED: "RUN_STARTED",
    EventType.RUN_FINISHED: "RUN_FINISHED",
    EventType.STEP_STARTED: "STEP_STARTED",
    EventType.STEP_FINISHED: "STEP_FINISHED",
    EventType.TEXT_MESSAGE: "TEXT_MESSAGE_CONTENT",
    EventType.TOOL_CALL: "TOOL_CALL_END",
    EventType.PERMISSION_DENIAL: "PERMISSION_DENIAL",
    EventType.BUDGET_EXCEEDED: "BUDGET_EXCEEDED",
    EventType.ERROR: "ERROR",
    EventType.TASK_SPAWNED: "TASK_SPAWNED",
    EventType.TASK_PROGRESS: "TASK_PROGRESS",
    EventType.QUEUE_STATUS: "QUEUE_STATUS",
    EventType.APPROVAL_PENDING: "APPROVAL_PENDING",
}


def stream_event_to_legacy_sse(event: StreamEvent) -> dict[str, Any]:
    """Convert a StreamEvent into the original flat JSON shape."""
    raw = asdict(event)
    raw["type"] = _LEGACY_TYPE_MAP.get(raw.get("event_type", ""), "UNKNOWN")
    return raw


class LegacySseFormatter(BaseSseFormatter):
    """Formatter that preserves the historical SSE payload shape."""

    def serialize(self, event: StreamEvent) -> list[dict[str, Any]]:
        return [stream_event_to_legacy_sse(event)]
