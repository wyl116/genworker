# edition: baseline
"""Tests for streaming events and SSE protocol adapters."""
import json

import pytest

from src.streaming.adapters.langgraph_adapter import LangGraphStreamAdapter
from src.streaming.event_adapter import (
    SUPPORTED_SSE_PROTOCOLS,
    create_sse_formatter,
    format_sse_line,
    stream_event_to_sse,
)
from src.streaming.events import (
    ApprovalPendingEvent,
    BudgetExceededEvent,
    ErrorEvent,
    EventType,
    PermissionDenialEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    StreamEvent,
    TextMessageEvent,
    ToolCallEvent,
    QueueStatusEvent,
)


class TestEventsFrozen:
    """All events must be frozen dataclasses."""

    def test_run_started_immutable(self):
        e = RunStartedEvent(run_id="r1")
        with pytest.raises(AttributeError):
            e.run_id = "r2"

    def test_text_message_immutable(self):
        e = TextMessageEvent(run_id="r1", content="hello")
        with pytest.raises(AttributeError):
            e.content = "modified"

    def test_tool_call_immutable(self):
        e = ToolCallEvent(run_id="r1", tool_name="t1")
        with pytest.raises(AttributeError):
            e.tool_name = "t2"

    def test_error_event_immutable(self):
        e = ErrorEvent(run_id="r1", code="ERR", message="fail")
        with pytest.raises(AttributeError):
            e.message = "changed"


class TestEventTypes:
    """Events must have correct event_type set automatically."""

    def test_run_started_type(self):
        e = RunStartedEvent(run_id="r1")
        assert e.event_type == EventType.RUN_STARTED

    def test_run_finished_type(self):
        e = RunFinishedEvent(run_id="r1")
        assert e.event_type == EventType.RUN_FINISHED

    def test_step_started_type(self):
        e = StepStartedEvent(run_id="r1", step_name="s1")
        assert e.event_type == EventType.STEP_STARTED

    def test_text_message_type(self):
        e = TextMessageEvent(run_id="r1", content="hi")
        assert e.event_type == EventType.TEXT_MESSAGE

    def test_tool_call_type(self):
        e = ToolCallEvent(run_id="r1", tool_name="t1")
        assert e.event_type == EventType.TOOL_CALL

    def test_budget_exceeded_type(self):
        e = BudgetExceededEvent(run_id="r1")
        assert e.event_type == EventType.BUDGET_EXCEEDED

    def test_error_type(self):
        e = ErrorEvent(run_id="r1")
        assert e.event_type == EventType.ERROR


class TestLegacyEventAdapter:
    """Legacy adapter keeps the historical flat payload format."""

    def test_run_started_to_sse(self):
        e = RunStartedEvent(run_id="r1")
        sse = stream_event_to_sse(e)
        assert sse["type"] == "RUN_STARTED"
        assert sse["run_id"] == "r1"

    def test_text_message_to_sse(self):
        e = TextMessageEvent(run_id="r1", content="hello world")
        sse = stream_event_to_sse(e)
        assert sse["type"] == "TEXT_MESSAGE_CONTENT"
        assert sse["content"] == "hello world"

    def test_tool_call_to_sse(self):
        e = ToolCallEvent(
            run_id="r1",
            tool_name="sql_executor",
            tool_input={"query": "SELECT 1"},
            tool_result="result",
        )
        sse = stream_event_to_sse(e)
        assert sse["type"] == "TOOL_CALL_END"
        assert sse["tool_name"] == "sql_executor"
        assert sse["tool_input"] == {"query": "SELECT 1"}

    def test_error_to_sse(self):
        e = ErrorEvent(run_id="r1", code="ENGINE_ERROR", message="boom")
        sse = stream_event_to_sse(e)
        assert sse["type"] == "ERROR"
        assert sse["code"] == "ENGINE_ERROR"
        assert sse["message"] == "boom"

    def test_permission_denial_to_sse(self):
        e = PermissionDenialEvent(run_id="r1", tool_name="admin", reason="denied")
        sse = stream_event_to_sse(e)
        assert sse["type"] == "PERMISSION_DENIAL"

    def test_budget_exceeded_to_sse(self):
        e = BudgetExceededEvent(run_id="r1", max_tokens=1000, used_tokens=1200)
        sse = stream_event_to_sse(e)
        assert sse["type"] == "BUDGET_EXCEEDED"
        assert sse["stop_reason"] == "budget_exceeded"

    def test_approval_pending_to_sse(self):
        e = ApprovalPendingEvent(
            run_id="r1",
            thread_id="thread-1",
            inbox_id="inbox-1",
            prompt="请审批",
        )
        sse = stream_event_to_sse(e)
        assert sse["type"] == "APPROVAL_PENDING"
        assert sse["thread_id"] == "thread-1"
        assert sse["inbox_id"] == "inbox-1"
        assert sse["prompt"] == "请审批"


class TestSSELineFormat:
    """SSE line formatting produces valid Server-Sent Events."""

    def test_format_sse_line(self):
        e = RunStartedEvent(run_id="test-run")
        line = format_sse_line(e)
        assert line.startswith("data: ")
        assert line.endswith("\n\n")

    def test_sse_line_is_valid_json(self):
        e = TextMessageEvent(run_id="r1", content="hello")
        line = format_sse_line(e)
        json_str = line.removeprefix("data: ").rstrip()
        data = json.loads(json_str)
        assert data["content"] == "hello"

    def test_sse_line_unicode(self):
        e = TextMessageEvent(run_id="r1", content="你好世界")
        line = format_sse_line(e)
        assert "你好世界" in line


class TestEventTimestamps:
    """Events have ISO 8601 timestamps."""

    def test_has_timestamp(self):
        e = RunStartedEvent(run_id="r1")
        assert e.timestamp is not None
        assert "T" in e.timestamp  # ISO 8601

    def test_different_events_have_timestamps(self):
        events = [
            RunStartedEvent(run_id="r1"),
            TextMessageEvent(run_id="r1", content="x"),
            RunFinishedEvent(run_id="r1"),
        ]
        for e in events:
            assert hasattr(e, "timestamp")
            assert len(e.timestamp) > 0


class TestProtocolFactory:
    """Protocol factory creates the expected formatter implementation."""

    def test_supported_protocols_include_ag_ui_and_legacy(self):
        assert SUPPORTED_SSE_PROTOCOLS == ("ag-ui", "legacy")

    def test_unknown_protocol_raises(self):
        with pytest.raises(ValueError):
            create_sse_formatter("unknown")


class TestAgUiFormatter:
    """AG-UI formatter expands internal events into protocol event sequences."""

    def test_text_message_emits_start_content_end(self):
        formatter = create_sse_formatter("ag-ui", thread_id="thread-1")
        event = TextMessageEvent(run_id="r1", content="hello world")

        payloads = [
            json.loads(line.removeprefix("data: ").rstrip())
            for line in formatter.format(event)
        ]

        assert [payload["type"] for payload in payloads] == [
            "TEXT_MESSAGE_START",
            "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END",
        ]
        assert payloads[0]["role"] == "assistant"
        assert payloads[1]["delta"] == "hello world"
        assert payloads[0]["messageId"] == payloads[1]["messageId"] == payloads[2]["messageId"]

    def test_tool_call_emits_start_args_end_result(self):
        formatter = create_sse_formatter("ag-ui")
        event = ToolCallEvent(
            run_id="r1",
            tool_name="sql_executor",
            tool_input={"query": "SELECT 1"},
            tool_result="1",
        )

        payloads = [
            json.loads(line.removeprefix("data: ").rstrip())
            for line in formatter.format(event)
        ]

        assert [payload["type"] for payload in payloads] == [
            "TOOL_CALL_START",
            "TOOL_CALL_ARGS",
            "TOOL_CALL_END",
            "TOOL_CALL_RESULT",
        ]
        assert payloads[0]["toolCallName"] == "sql_executor"
        assert payloads[1]["delta"] == '{"query": "SELECT 1"}'
        assert payloads[0]["toolCallId"] == payloads[1]["toolCallId"] == payloads[2]["toolCallId"]
        assert payloads[3]["toolCallId"] == payloads[0]["toolCallId"]
        assert payloads[3]["content"] == "1"

    def test_run_started_uses_ag_ui_shape(self):
        formatter = create_sse_formatter("ag-ui", thread_id="thread-xyz")
        payload = json.loads(
            formatter.format(RunStartedEvent(run_id="r1"))[0].removeprefix("data: ").rstrip()
        )

        assert payload["type"] == "RUN_STARTED"
        assert payload["runId"] == "r1"
        assert payload["threadId"] == "thread-xyz"
        assert isinstance(payload["timestamp"], int)

    def test_error_maps_to_run_error(self):
        formatter = create_sse_formatter("ag-ui")
        payload = json.loads(
            formatter.format(ErrorEvent(run_id="r1", code="ENGINE_ERROR", message="boom"))[0]
            .removeprefix("data: ").rstrip()
        )

        assert payload["type"] == "RUN_ERROR"
        assert payload["code"] == "ENGINE_ERROR"
        assert payload["message"] == "boom"

    def test_queue_status_maps_to_custom_event(self):
        formatter = create_sse_formatter("ag-ui")
        payload = json.loads(
            formatter.format(
                QueueStatusEvent(
                    run_id="queue-1",
                    thread_id="thread-1",
                    tenant_id="demo",
                    worker_id="svc-1",
                    status="queued",
                    position=1,
                    queue_size=2,
                )
            )[0].removeprefix("data: ").rstrip()
        )

        assert payload["type"] == "CUSTOM"
        assert payload["name"] == "queue_status"
        assert payload["value"]["status"] == "queued"

    def test_approval_pending_maps_to_custom_event(self):
        formatter = create_sse_formatter("ag-ui")
        payload = json.loads(
            formatter.format(
                ApprovalPendingEvent(
                    run_id="r1",
                    thread_id="thread-1",
                    inbox_id="inbox-1",
                    prompt="请审批",
                )
            )[0].removeprefix("data: ").rstrip()
        )

        assert payload["type"] == "CUSTOM"
        assert payload["name"] == "approval_pending"
        assert payload["value"]["runId"] == "r1"
        assert payload["value"]["threadId"] == "thread-1"
        assert payload["value"]["inboxId"] == "inbox-1"
        assert payload["value"]["prompt"] == "请审批"


class TestLangGraphStreamAdapter:
    @pytest.mark.asyncio
    async def test_adapt_maps_stream_and_tool_events(self):
        adapter = LangGraphStreamAdapter()

        async def _source():
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": type("Chunk", (), {"content": "hello"})()},
            }
            yield {
                "event": "on_tool_end",
                "name": "lookup",
                "data": {"output": "done"},
            }

        events = [event async for event in adapter.adapt(_source(), run_id="r1")]

        assert isinstance(events[0], RunStartedEvent)
        assert isinstance(events[1], TextMessageEvent)
        assert events[1].content == "hello"
        assert isinstance(events[2], ToolCallEvent)
        assert events[2].tool_name == "lookup"
        assert events[2].tool_result == "done"
        assert isinstance(events[3], RunFinishedEvent)
