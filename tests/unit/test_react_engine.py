# edition: baseline
"""
ReactEngine unit tests.

All tests use mock LLMClient/ToolExecutor - no real LLM or external services.
"""
from __future__ import annotations

import pytest

from src.engine.protocols import LLMClient, LLMResponse, ToolCall, ToolExecutor, ToolResult, UsageInfo
from src.engine.react.agent import ReactEngine
from src.engine.state import UsageBudget
from src.services.llm.intent import Purpose
from src.streaming.events import (
    BudgetExceededEvent,
    ErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageEvent,
    ToolCallEvent,
)


# ---------------------------------------------------------------------------
# Mock implementations
# ---------------------------------------------------------------------------

class MockLLMClient:
    """LLMClient Protocol mock that returns pre-configured responses."""

    def __init__(self, responses: list[LLMResponse] | None = None) -> None:
        self._responses = list(responses or [])
        self._call_count = 0
        self.intents = []

    async def invoke(
        self,
        messages,
        tools=None,
        tool_choice=None,
        system_blocks=None,
        intent=None,
    ) -> LLMResponse:
        self.intents.append(intent)
        if self._call_count < len(self._responses):
            resp = self._responses[self._call_count]
            self._call_count += 1
            return resp
        return LLMResponse(content="default response")


class ErrorLLMClient:
    """LLMClient that always raises."""

    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None) -> LLMResponse:
        raise RuntimeError("LLM timeout")


class MockToolExecutor:
    """ToolExecutor Protocol mock."""

    def __init__(self, results: dict[str, ToolResult] | None = None) -> None:
        self._results = results or {}
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, tool_name: str, tool_input: dict) -> ToolResult:
        self.calls.append((tool_name, tool_input))
        return self._results.get(tool_name, ToolResult(content=f"result of {tool_name}"))


class ErrorToolExecutor:
    """ToolExecutor that always raises."""

    async def execute(self, tool_name: str, tool_input: dict) -> ToolResult:
        raise RuntimeError(f"Tool {tool_name} crashed")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

async def _collect_events(engine: ReactEngine, **kwargs) -> list:
    events = []
    async for event in engine.execute(**kwargs):
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_react_loop_with_mock_llm():
    """Simple text response - no tool calls."""
    llm = MockLLMClient([
        LLMResponse(content="Hello!", usage=UsageInfo(total_tokens=10)),
    ])
    engine = ReactEngine(llm_client=llm, tool_executor=MockToolExecutor())

    events = await _collect_events(
        engine, system_prompt="You are helpful.", task="Hi"
    )

    types = [type(e) for e in events]
    assert RunStartedEvent in types
    assert TextMessageEvent in types
    assert RunFinishedEvent in types

    text_events = [e for e in events if isinstance(e, TextMessageEvent)]
    assert text_events[0].content == "Hello!"

    finish = [e for e in events if isinstance(e, RunFinishedEvent)][0]
    assert finish.success is True
    assert llm.intents[0].purpose is Purpose.CHAT_TURN
    assert llm.intents[0].requires_tools is False


@pytest.mark.asyncio
async def test_react_loop_with_tool_calls():
    """LLM calls a tool, then produces final text."""
    llm = MockLLMClient([
        LLMResponse(
            content="",
            tool_calls=(ToolCall(tool_name="search", tool_input={"q": "test"}, tool_call_id="tc1"),),
            usage=UsageInfo(total_tokens=20),
        ),
        LLMResponse(content="Found result.", usage=UsageInfo(total_tokens=15)),
    ])
    executor = MockToolExecutor({"search": ToolResult(content="search result")})
    engine = ReactEngine(llm_client=llm, tool_executor=executor)

    events = await _collect_events(
        engine, system_prompt="sys", task="search something"
    )

    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].tool_name == "search"
    assert tool_events[0].tool_result == "search result"
    assert not tool_events[0].is_error

    text_events = [e for e in events if isinstance(e, TextMessageEvent)]
    assert text_events[0].content == "Found result."

    assert executor.calls == [("search", {"q": "test"})]
    assert llm.intents[0].requires_tools is False


@pytest.mark.asyncio
async def test_react_loop_marks_requires_tools_when_tools_are_available():
    llm = MockLLMClient([
        LLMResponse(content="Hello!", usage=UsageInfo(total_tokens=10)),
    ])
    engine = ReactEngine(llm_client=llm, tool_executor=MockToolExecutor())

    events = await _collect_events(
        engine,
        system_prompt="sys",
        task="Hi",
        tools=[{
            "type": "function",
            "function": {
                "name": "search",
                "description": "Search",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
    )

    assert any(isinstance(event, TextMessageEvent) for event in events)
    assert llm.intents[0].requires_tools is True


@pytest.mark.asyncio
async def test_max_rounds_limit():
    """Engine stops after max_rounds even if LLM keeps calling tools."""
    # Each round returns a tool call, so we need max_rounds tool-call responses
    # plus one final response after max_rounds is reached (called without tools).
    tool_responses = [
        LLMResponse(
            content="",
            tool_calls=(ToolCall(tool_name="loop_tool", tool_input={}, tool_call_id=f"tc{i}"),),
            usage=UsageInfo(total_tokens=5),
        )
        for i in range(5)
    ]
    # After max_rounds exhausted, engine calls LLM once more without tools
    final_response = LLMResponse(content="Finally done.", usage=UsageInfo(total_tokens=5))
    llm = MockLLMClient(tool_responses + [final_response])
    engine = ReactEngine(llm_client=llm, tool_executor=MockToolExecutor(), max_rounds=3)

    events = await _collect_events(
        engine, system_prompt="sys", task="loop"
    )

    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 3  # exactly max_rounds tool calls

    finish = [e for e in events if isinstance(e, RunFinishedEvent)][0]
    assert finish.stop_reason == "max_rounds"


@pytest.mark.asyncio
async def test_budget_exceeded_yields_event_not_exception():
    """Budget exceeded yields BudgetExceededEvent, does not raise."""
    llm = MockLLMClient([
        LLMResponse(content="round 1", usage=UsageInfo(total_tokens=100)),
    ])
    engine = ReactEngine(llm_client=llm, tool_executor=MockToolExecutor())

    # Budget of 50, but first response uses 100 tokens
    # The engine checks budget BEFORE the next round, so after round 1
    # the budget will be exceeded and the next iteration yields the event.
    # However, since round 1 has no tool_calls, it returns immediately.
    # To trigger the budget path, we need tool calls in round 1.
    llm2 = MockLLMClient([
        LLMResponse(
            content="",
            tool_calls=(ToolCall(tool_name="t", tool_input={}, tool_call_id="tc1"),),
            usage=UsageInfo(total_tokens=60),
        ),
        # This response won't be reached because budget is exceeded
        LLMResponse(content="unreachable", usage=UsageInfo(total_tokens=10)),
    ])
    engine2 = ReactEngine(llm_client=llm2, tool_executor=MockToolExecutor())

    events = await _collect_events(
        engine2, system_prompt="sys", task="t", budget=UsageBudget(max_tokens=50)
    )

    budget_events = [e for e in events if isinstance(e, BudgetExceededEvent)]
    assert len(budget_events) == 1
    assert budget_events[0].max_tokens == 50

    finish = [e for e in events if isinstance(e, RunFinishedEvent)][0]
    assert finish.stop_reason == "budget_exceeded"


@pytest.mark.asyncio
async def test_llm_error_yields_error_event():
    """LLM invocation failure yields ErrorEvent, no exception."""
    engine = ReactEngine(llm_client=ErrorLLMClient(), tool_executor=MockToolExecutor())

    events = await _collect_events(
        engine, system_prompt="sys", task="fail"
    )

    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(error_events) == 1
    assert "LLM timeout" in error_events[0].message

    finish = [e for e in events if isinstance(e, RunFinishedEvent)][0]
    assert finish.success is False
    assert finish.stop_reason == "llm_error"


@pytest.mark.asyncio
async def test_tool_execution_failure_continues():
    """Tool execution failure is captured and returned as error result."""
    llm = MockLLMClient([
        LLMResponse(
            content="",
            tool_calls=(ToolCall(tool_name="bad_tool", tool_input={}, tool_call_id="tc1"),),
            usage=UsageInfo(total_tokens=10),
        ),
        LLMResponse(content="Recovered.", usage=UsageInfo(total_tokens=10)),
    ])
    engine = ReactEngine(llm_client=llm, tool_executor=ErrorToolExecutor())

    events = await _collect_events(
        engine, system_prompt="sys", task="test"
    )

    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].is_error is True
    assert "crashed" in tool_events[0].tool_result

    # Engine should continue and get final text
    text_events = [e for e in events if isinstance(e, TextMessageEvent)]
    assert text_events[0].content == "Recovered."


@pytest.mark.asyncio
async def test_run_id_propagated():
    """All events carry the provided run_id."""
    llm = MockLLMClient([LLMResponse(content="ok")])
    engine = ReactEngine(llm_client=llm, tool_executor=MockToolExecutor())

    events = await _collect_events(
        engine, system_prompt="sys", task="t", run_id="test-run-123"
    )

    for event in events:
        assert event.run_id == "test-run-123"


@pytest.mark.asyncio
async def test_multiple_tool_calls_in_single_round():
    """LLM requests multiple tool calls in one response."""
    llm = MockLLMClient([
        LLMResponse(
            content="",
            tool_calls=(
                ToolCall(tool_name="tool_a", tool_input={"x": 1}, tool_call_id="tc1"),
                ToolCall(tool_name="tool_b", tool_input={"y": 2}, tool_call_id="tc2"),
            ),
            usage=UsageInfo(total_tokens=20),
        ),
        LLMResponse(content="Both done.", usage=UsageInfo(total_tokens=10)),
    ])
    executor = MockToolExecutor({
        "tool_a": ToolResult(content="a-result"),
        "tool_b": ToolResult(content="b-result"),
    })
    engine = ReactEngine(llm_client=llm, tool_executor=executor)

    events = await _collect_events(
        engine, system_prompt="sys", task="multi"
    )

    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 2
    assert {e.tool_name for e in tool_events} == {"tool_a", "tool_b"}


@pytest.mark.asyncio
async def test_empty_content_no_text_event():
    """When LLM returns empty content, no TextMessageEvent is yielded."""
    llm = MockLLMClient([LLMResponse(content="")])
    engine = ReactEngine(llm_client=llm, tool_executor=MockToolExecutor())

    events = await _collect_events(
        engine, system_prompt="sys", task="t"
    )

    text_events = [e for e in events if isinstance(e, TextMessageEvent)]
    assert len(text_events) == 0

    # Still finishes successfully
    finish = [e for e in events if isinstance(e, RunFinishedEvent)][0]
    assert finish.success is True
