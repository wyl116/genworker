# edition: baseline
"""
WorkflowEngine unit tests.

Tests deterministic sequential step execution with retry support.
All tests use mock LLMClient/ToolExecutor.
"""
from __future__ import annotations

import pytest

from src.engine.protocols import LLMClient, LLMResponse, ToolCall, ToolExecutor, ToolResult, UsageInfo
from src.engine.state import UsageBudget
from src.engine.workflow.engine import WorkflowEngine
from src.services.llm.intent import Purpose
from src.skills.models import RetryConfig, WorkflowStep, WorkflowStepType
from src.streaming.events import (
    BudgetExceededEvent,
    ErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    StreamEvent,
    TextMessageEvent,
    ToolCallEvent,
)


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------

class MockLLMClient:
    """Returns pre-configured responses in sequence."""

    def __init__(self, responses: list[LLMResponse] | None = None) -> None:
        self._responses = list(responses or [])
        self._call_count = 0
        self.intents = []

    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None) -> LLMResponse:
        self.intents.append(intent)
        if self._call_count < len(self._responses):
            resp = self._responses[self._call_count]
            self._call_count += 1
            return resp
        return LLMResponse(content="default")


class FailThenSucceedLLM:
    """Fails N times then succeeds (for retry testing)."""

    def __init__(self, fail_count: int, success_response: LLMResponse) -> None:
        self._fail_count = fail_count
        self._success = success_response
        self._call_count = 0

    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None) -> LLMResponse:
        self._call_count += 1
        if self._call_count <= self._fail_count:
            raise RuntimeError(f"Attempt {self._call_count} failed")
        return self._success


class MockToolExecutor:
    def __init__(self, results: dict[str, ToolResult] | None = None) -> None:
        self._results = results or {}
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, tool_name: str, tool_input: dict) -> ToolResult:
        self.calls.append((tool_name, tool_input))
        return self._results.get(tool_name, ToolResult(content=f"result:{tool_name}"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_steps(*names: str) -> tuple[WorkflowStep, ...]:
    """Create simple deterministic steps."""
    return tuple(
        WorkflowStep(step=name, type=WorkflowStepType.DETERMINISTIC)
        for name in names
    )


def _simple_prompt_builder(step, previous_input):
    return f"Execute step: {step.step}. Input: {previous_input}"


async def _collect(engine: WorkflowEngine, **kwargs) -> list:
    events = []
    async for event in engine.execute(**kwargs):
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deterministic_three_step_flow():
    """Three steps execute in sequence, each producing output."""
    llm = MockLLMClient([
        LLMResponse(content="planning output"),
        LLMResponse(content="execution output"),
        LLMResponse(content="summary output"),
    ])
    engine = WorkflowEngine(llm_client=llm, tool_executor=MockToolExecutor())
    steps = _make_steps("planning", "execution", "summarization")

    events = await _collect(
        engine,
        steps=steps,
        task="Analyze sales data",
        build_step_prompt=_simple_prompt_builder,
    )

    # Check lifecycle events
    step_started = [e for e in events if isinstance(e, StepStartedEvent)]
    step_finished = [e for e in events if isinstance(e, StepFinishedEvent)]
    assert len(step_started) == 3
    assert len(step_finished) == 3
    assert [e.step_name for e in step_started] == ["planning", "execution", "summarization"]
    assert all(e.success for e in step_finished)

    # Check text output
    text_events = [e for e in events if isinstance(e, TextMessageEvent)]
    assert len(text_events) == 3
    assert text_events[0].content == "planning output"
    assert text_events[2].content == "summary output"

    # Run lifecycle
    assert isinstance(events[0], RunStartedEvent)
    finish = [e for e in events if isinstance(e, RunFinishedEvent)][0]
    assert finish.success is True
    assert llm.intents[0].purpose is Purpose.TOOL_CALL


@pytest.mark.asyncio
async def test_retry_on_step_failure():
    """Step retries on failure and eventually succeeds."""
    llm = FailThenSucceedLLM(
        fail_count=2,
        success_response=LLMResponse(content="success after retries"),
    )
    engine = WorkflowEngine(llm_client=llm, tool_executor=MockToolExecutor())

    steps = (
        WorkflowStep(
            step="flaky_step",
            type=WorkflowStepType.DETERMINISTIC,
            retry=RetryConfig(max_attempts=3),
        ),
    )

    events = await _collect(
        engine,
        steps=steps,
        task="test retry",
        build_step_prompt=_simple_prompt_builder,
    )

    step_finished = [e for e in events if isinstance(e, StepFinishedEvent)]
    assert len(step_finished) == 1
    assert step_finished[0].success is True

    text_events = [e for e in events if isinstance(e, TextMessageEvent)]
    assert text_events[0].content == "success after retries"


@pytest.mark.asyncio
async def test_step_failure_after_exhausted_retries():
    """Step fails permanently when retries exhausted."""
    llm = FailThenSucceedLLM(
        fail_count=10,  # more fails than retries
        success_response=LLMResponse(content="never reached"),
    )
    engine = WorkflowEngine(llm_client=llm, tool_executor=MockToolExecutor())

    steps = (
        WorkflowStep(
            step="failing_step",
            type=WorkflowStepType.DETERMINISTIC,
            retry=RetryConfig(max_attempts=2),
        ),
    )

    events = await _collect(
        engine,
        steps=steps,
        task="test fail",
        build_step_prompt=_simple_prompt_builder,
    )

    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(error_events) == 1
    assert "failing_step" in error_events[0].message

    step_finished = [e for e in events if isinstance(e, StepFinishedEvent)]
    assert step_finished[0].success is False

    finish = [e for e in events if isinstance(e, RunFinishedEvent)][0]
    assert finish.success is False
    assert finish.stop_reason == "step_failed"


@pytest.mark.asyncio
async def test_step_with_tool_choice():
    """Single tool in step triggers tool_choice constraint."""
    llm = MockLLMClient([
        LLMResponse(
            content="",
            tool_calls=(ToolCall(tool_name="query_db", tool_input={"sql": "SELECT 1"}, tool_call_id="tc1"),),
        ),
    ])
    executor = MockToolExecutor({"query_db": ToolResult(content="1 row returned")})
    engine = WorkflowEngine(llm_client=llm, tool_executor=executor)

    steps = (
        WorkflowStep(
            step="query",
            type=WorkflowStepType.DETERMINISTIC,
            tools=("query_db",),
        ),
    )

    events = await _collect(
        engine,
        steps=steps,
        task="run query",
        build_step_prompt=_simple_prompt_builder,
        available_tools=[{"function": {"name": "query_db"}, "type": "function"}],
    )

    step_finished = [e for e in events if isinstance(e, StepFinishedEvent)]
    assert step_finished[0].success is True

    assert executor.calls == [("query_db", {"sql": "SELECT 1"})]


@pytest.mark.asyncio
async def test_budget_exceeded_stops_workflow():
    """Budget exceeded between steps stops the workflow."""
    llm = MockLLMClient([
        LLMResponse(content="step 1 done"),
        LLMResponse(content="step 2 unreachable"),
    ])
    engine = WorkflowEngine(llm_client=llm, tool_executor=MockToolExecutor())
    steps = _make_steps("step1", "step2")

    # Budget of 0 with max_tokens=1 means it's already exceeded
    events = await _collect(
        engine,
        steps=steps,
        task="t",
        build_step_prompt=_simple_prompt_builder,
        budget=UsageBudget(max_tokens=1, used_tokens=10),
    )

    budget_events = [e for e in events if isinstance(e, BudgetExceededEvent)]
    assert len(budget_events) == 1

    finish = [e for e in events if isinstance(e, RunFinishedEvent)][0]
    assert finish.stop_reason == "budget_exceeded"


@pytest.mark.asyncio
async def test_step_data_passing():
    """Output from step N becomes input to step N+1."""
    captured_inputs = []

    def tracking_prompt_builder(step, previous_input):
        captured_inputs.append((step.step, previous_input))
        return f"Step: {step.step}"

    llm = MockLLMClient([
        LLMResponse(content="output_from_step1"),
        LLMResponse(content="output_from_step2"),
    ])
    engine = WorkflowEngine(llm_client=llm, tool_executor=MockToolExecutor())
    steps = _make_steps("step1", "step2")

    await _collect(
        engine,
        steps=steps,
        task="initial_task",
        build_step_prompt=tracking_prompt_builder,
    )

    # step1 receives original task, step2 receives step1's output
    assert captured_inputs[0] == ("step1", "initial_task")
    assert captured_inputs[1] == ("step2", "output_from_step1")


@pytest.mark.asyncio
async def test_empty_steps_finishes_immediately():
    """Empty step list finishes run immediately."""
    llm = MockLLMClient([])
    engine = WorkflowEngine(llm_client=llm, tool_executor=MockToolExecutor())

    events = await _collect(
        engine,
        steps=(),
        task="t",
        build_step_prompt=_simple_prompt_builder,
    )

    assert isinstance(events[0], RunStartedEvent)
    finish = [e for e in events if isinstance(e, RunFinishedEvent)][0]
    assert finish.success is True
