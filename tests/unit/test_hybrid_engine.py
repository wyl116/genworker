# edition: baseline
"""Tests for HybridEngine - autonomous/deterministic step switching and data passing."""
import pytest

from src.engine.hybrid.engine import HybridEngine
from src.engine.protocols import LLMResponse, ToolCall, ToolResult, UsageInfo
from src.engine.state import UsageBudget
from src.skills.models import RetryConfig, WorkflowStep, WorkflowStepType
from src.streaming.events import (
    ErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageEvent,
)


# --- Helpers ---

class MockLLM:
    """Mock LLMClient that returns a fixed response."""

    def __init__(self, content: str = "mock response", tool_calls=()):
        self._content = content
        self._tool_calls = tuple(tool_calls)

    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
        return LLMResponse(
            content=self._content,
            tool_calls=self._tool_calls,
            usage=UsageInfo(total_tokens=10),
        )


class SequentialMockLLM:
    """Mock LLM that returns different responses in sequence."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self._index = 0

    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
        resp = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return resp


class MockToolExecutor:
    async def execute(self, tool_name, tool_input):
        return ToolResult(content=f"result of {tool_name}")


def _step(name, step_type, instruction_ref="", tools=(), max_rounds=None, retry=None):
    return WorkflowStep(
        step=name,
        type=WorkflowStepType(step_type),
        instruction_ref=instruction_ref,
        tools=tuple(tools),
        max_rounds=max_rounds,
        retry=retry or RetryConfig(),
    )


async def _collect(engine, steps, task, **kwargs):
    events = []
    async for e in engine.execute(
        steps=steps,
        task=task,
        build_step_prompt=lambda step, prev: f"prompt for {step.step}: {prev}",
        build_autonomous_prompt=lambda step: f"auto prompt for {step.step}",
        **kwargs,
    ):
        events.append(e)
    return events


# --- Tests ---

@pytest.mark.asyncio
async def test_step_type_switching():
    """Hybrid engine executes autonomous and deterministic steps in sequence."""
    engine = HybridEngine(
        llm_client=MockLLM(content="step output"),
        tool_executor=MockToolExecutor(),
    )
    steps = (
        _step("planning", "autonomous", max_rounds=2),
        _step("execution", "deterministic", tools=("sql_executor",)),
        _step("summarization", "autonomous", max_rounds=2),
    )

    events = await _collect(engine, steps, "analyze data")

    event_types = [type(e).__name__ for e in events]
    assert event_types[0] == "RunStartedEvent"
    assert event_types[-1] == "RunFinishedEvent"

    step_names = [e.step_name for e in events if isinstance(e, StepStartedEvent)]
    assert step_names == ["planning", "execution", "summarization"]

    finished_names = [e.step_name for e in events if isinstance(e, StepFinishedEvent)]
    assert finished_names == ["planning", "execution", "summarization"]


@pytest.mark.asyncio
async def test_step_result_data_passing():
    """Steps pass data via StepResult.as_input - next step receives previous output."""
    call_log = []

    def build_step_prompt(step, prev_input):
        call_log.append(("det", step.step, prev_input))
        return f"prompt: {prev_input}"

    def build_auto_prompt(step):
        call_log.append(("auto", step.step))
        return "auto prompt"

    engine = HybridEngine(
        llm_client=MockLLM(content="auto result"),
        tool_executor=MockToolExecutor(),
    )
    steps = (
        _step("plan", "autonomous", max_rounds=1),
        _step("exec", "deterministic"),
    )

    events = []
    async for e in engine.execute(
        steps=steps,
        task="initial task",
        build_step_prompt=build_step_prompt,
        build_autonomous_prompt=build_auto_prompt,
    ):
        events.append(e)

    # The deterministic step should receive the autonomous step's output
    det_calls = [entry for entry in call_log if entry[0] == "det"]
    assert len(det_calls) == 1
    assert det_calls[0][2] == "auto result"  # previous step output passed


@pytest.mark.asyncio
async def test_empty_steps_finishes_immediately():
    """Empty workflow finishes with RunStarted + RunFinished."""
    engine = HybridEngine(
        llm_client=MockLLM(),
        tool_executor=MockToolExecutor(),
    )
    events = await _collect(engine, steps=(), task="test")
    assert isinstance(events[0], RunStartedEvent)
    assert isinstance(events[-1], RunFinishedEvent)
    assert events[-1].success is True


@pytest.mark.asyncio
async def test_deterministic_step_failure_stops_engine():
    """If a deterministic step fails, engine stops with error events."""

    class FailingLLM:
        async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
            raise RuntimeError("LLM error")

    engine = HybridEngine(
        llm_client=FailingLLM(),
        tool_executor=MockToolExecutor(),
    )
    steps = (_step("exec", "deterministic"),)
    events = await _collect(engine, steps, "test")

    has_error = any(isinstance(e, ErrorEvent) for e in events)
    assert has_error
    assert isinstance(events[-1], RunFinishedEvent)
    assert events[-1].success is False


@pytest.mark.asyncio
async def test_budget_exceeded_stops_hybrid():
    """Budget exceeded before step starts → stop with BudgetExceededEvent."""
    engine = HybridEngine(
        llm_client=MockLLM(),
        tool_executor=MockToolExecutor(),
    )
    steps = (_step("plan", "autonomous"),)
    budget = UsageBudget(max_tokens=100, used_tokens=200)

    events = await _collect(engine, steps, "test", budget=budget)
    event_types = [type(e).__name__ for e in events]
    assert "BudgetExceededEvent" in event_types
    assert isinstance(events[-1], RunFinishedEvent)
    assert events[-1].stop_reason == "budget_exceeded"


@pytest.mark.asyncio
async def test_text_message_emitted_per_step():
    """Each step with content emits a TextMessageEvent."""
    engine = HybridEngine(
        llm_client=MockLLM(content="output text"),
        tool_executor=MockToolExecutor(),
    )
    steps = (
        _step("s1", "deterministic"),
        _step("s2", "deterministic"),
    )
    events = await _collect(engine, steps, "test")
    text_events = [e for e in events if isinstance(e, TextMessageEvent)]
    assert len(text_events) == 2
