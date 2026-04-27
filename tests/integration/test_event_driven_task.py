# edition: baseline
"""
Integration tests for event-driven task creation and streaming events.

Tests:
- TaskSpawnedEvent and TaskProgressEvent in StreamEvent Union
- EventBus task.failed subscription via SessionManager
- EventType enum new entries
- event_adapter mapping for new event types
"""
import json

import pytest

from src.events.bus import EventBus, Subscription
from src.events.models import Event
from src.streaming.event_adapter import format_sse_line, stream_event_to_sse
from src.streaming.events import (
    EventType,
    StreamEvent,
    TaskProgressEvent,
    TaskSpawnedEvent,
)


class TestStreamEventUnionExtension:
    """TaskSpawnedEvent and TaskProgressEvent are part of StreamEvent Union."""

    def test_task_spawned_event_frozen(self):
        event = TaskSpawnedEvent(
            run_id="r1",
            task_id="task-001",
            task_description="Generate report",
        )
        with pytest.raises(AttributeError):
            event.task_id = "modified"

    def test_task_progress_event_frozen(self):
        event = TaskProgressEvent(
            run_id="r1",
            task_id="task-001",
            progress=0.5,
            current_step="Fetching data",
        )
        with pytest.raises(AttributeError):
            event.progress = 1.0

    def test_task_spawned_event_type(self):
        event = TaskSpawnedEvent(run_id="r1", task_id="t1")
        assert event.event_type == EventType.TASK_SPAWNED

    def test_task_progress_event_type(self):
        event = TaskProgressEvent(run_id="r1", task_id="t1")
        assert event.event_type == EventType.TASK_PROGRESS

    def test_task_spawned_has_timestamp(self):
        event = TaskSpawnedEvent(run_id="r1", task_id="t1")
        assert event.timestamp
        assert "T" in event.timestamp

    def test_task_progress_defaults(self):
        event = TaskProgressEvent(run_id="r1")
        assert event.task_id == ""
        assert event.progress == 0.0
        assert event.current_step is None


class TestEventTypeEnum:
    """EventType enum has TASK_SPAWNED and TASK_PROGRESS."""

    def test_task_spawned_in_enum(self):
        assert EventType.TASK_SPAWNED == "TASK_SPAWNED"

    def test_task_progress_in_enum(self):
        assert EventType.TASK_PROGRESS == "TASK_PROGRESS"

    def test_all_original_types_still_present(self):
        expected = {
            "RUN_STARTED", "RUN_FINISHED", "STEP_STARTED", "STEP_FINISHED",
            "TEXT_MESSAGE", "TOOL_CALL", "PERMISSION_DENIAL",
            "BUDGET_EXCEEDED", "ERROR",
            "TASK_SPAWNED", "TASK_PROGRESS", "QUEUE_STATUS",
        }
        actual = {e.value for e in EventType}
        assert expected <= actual


class TestEventAdapterNewTypes:
    """event_adapter correctly maps new event types."""

    def test_task_spawned_to_sse(self):
        event = TaskSpawnedEvent(
            run_id="r1",
            task_id="task-abc",
            task_description="Run analysis",
        )
        sse = stream_event_to_sse(event)
        assert sse["type"] == "TASK_SPAWNED"
        assert sse["task_id"] == "task-abc"
        assert sse["task_description"] == "Run analysis"

    def test_task_progress_to_sse(self):
        event = TaskProgressEvent(
            run_id="r1",
            task_id="task-abc",
            progress=0.75,
            current_step="Processing",
        )
        sse = stream_event_to_sse(event)
        assert sse["type"] == "TASK_PROGRESS"
        assert sse["progress"] == 0.75
        assert sse["current_step"] == "Processing"

    def test_task_spawned_sse_line(self):
        event = TaskSpawnedEvent(
            run_id="r1",
            task_id="task-123",
            task_description="Test task",
        )
        line = format_sse_line(event)
        assert line.startswith("data: ")
        assert line.endswith("\n\n")
        data = json.loads(line.removeprefix("data: ").rstrip())
        assert data["type"] == "TASK_SPAWNED"
        assert data["task_id"] == "task-123"


class TestEventBusTaskFailed:
    """EventBus task.failed events are handled by SessionManager."""

    @pytest.mark.asyncio
    async def test_task_failed_event_recorded(self):
        """Publishing task.failed records failure in SessionManager."""
        from pathlib import Path
        import tempfile

        from src.conversation.session_manager import SessionManager
        from src.conversation.session_store import FileSessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = FileSessionStore(Path(tmp))
            manager = SessionManager(store=store)

            # Create a session first
            session = await manager.get_or_create("t1", "demo", "w1")
            session_id = session.session_id

            # Set up EventBus with task.failed subscription
            bus = EventBus()

            async def on_task_failed(event: Event) -> None:
                payload = dict(event.payload)
                manager.record_task_failure(
                    session_id=payload.get("session_id", ""),
                    error_message=payload.get("error_message", ""),
                )

            bus.subscribe(Subscription(
                handler_id="test_handler",
                event_type="task.failed",
                tenant_id="demo",
                handler=on_task_failed,
            ))

            # Publish task.failed event
            event = Event(
                event_id="evt-1",
                type="task.failed",
                source="task_runner",
                tenant_id="demo",
                payload=(
                    ("session_id", session_id),
                    ("task_id", "task-xyz"),
                    ("error_message", "Execution timeout"),
                    ("error_code", "TIMEOUT"),
                ),
            )
            triggered = await bus.publish(event)
            assert triggered == 1

            # Next get_or_create should inject the failure message
            session2 = await manager.get_or_create("t1", "demo", "w1")
            system_msgs = [m for m in session2.messages if m.role == "system"]
            assert len(system_msgs) == 1
            assert "Execution timeout" in system_msgs[0].content
