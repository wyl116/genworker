# edition: baseline
"""Integration tests for EnhancedPlanningExecutor - full planning loop."""
from __future__ import annotations

import json
from typing import Any

import pytest

from src.engine.protocols import LLMResponse
from src.engine.state import WorkerContext
from src.events.models import Event
from src.worker.planning.decomposer import Decomposer
from src.worker.planning.enhanced_executor import EnhancedPlanningExecutor
from src.worker.planning.models import SubGoal
from src.worker.planning.reflector import Reflector
from src.worker.planning.strategy_selector import StrategySelector
from src.worker.planning.subagent.executor import SubAgentExecutor
from src.worker.planning.subagent.models import SubAgentContext
from src.worker.scripts.models import InlineScript


# ---------------------------------------------------------------------------
# Mock components
# ---------------------------------------------------------------------------

class MockLLMClient:
    """LLM client that returns different responses per call."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._call_index = 0

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        system_blocks: list[dict[str, Any]] | None = None,
        intent=None,
    ) -> LLMResponse:
        if self._call_index < len(self._responses):
            content = self._responses[self._call_index]
        else:
            content = self._responses[-1]
        self._call_index += 1
        return LLMResponse(content=content)


class MockTaskExecutor:
    """Simple task executor returning static content."""

    def __init__(self, default_result: str = "task completed") -> None:
        self._default_result = default_result
        self.executed_agents: list[str] = []

    async def execute_subagent(self, context: SubAgentContext) -> str:
        self.executed_agents.append(context.agent_id)
        return f"{self._default_result}: {context.sub_goal.description}"


class MockEventBus:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> int:
        self.events.append(event)
        return 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decomposition_response(
    goals: list[dict[str, Any]],
    reasoning: str = "decomposed",
) -> str:
    return json.dumps({"sub_goals": goals, "reasoning": reasoning})


def _strategy_response(skill: str = "search", reason: str = "best fit") -> str:
    return json.dumps({
        "selected_skill": skill,
        "reason": reason,
        "delegate_to": None,
    })


def _reflection_response(
    score: int = 9,
    missing: list[str] | None = None,
    additional: list[dict[str, Any]] | None = None,
) -> str:
    return json.dumps({
        "completeness_score": score,
        "missing_aspects": missing or [],
        "additional_sub_goals": additional or [],
    })


def _make_worker_context(
    worker_id: str = "test-worker",
    tenant_id: str = "tenant-1",
    **overrides,
) -> WorkerContext:
    return WorkerContext(
        worker_id=worker_id,
        tenant_id=tenant_id,
        identity="TestWorker\nRole: tester",
        tool_names=("search", "analyze", "write"),
        available_skill_ids=("search", "analyze", "write"),
        **overrides,
    )


# ---------------------------------------------------------------------------
# Tests: full planning loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_planning_loop_single_iteration():
    """Complete planning loop: decompose -> select -> execute -> reflect (pass)."""
    decompose_resp = _decomposition_response([
        {"id": "sg-1", "description": "Search data", "skill_hint": "search"},
        {"id": "sg-2", "description": "Analyze results", "skill_hint": "analyze", "depends_on": ["sg-1"]},
    ])
    strategy_resp_1 = _strategy_response("search")
    strategy_resp_2 = _strategy_response("analyze")
    reflect_resp = _reflection_response(score=9)

    # LLM responses: decompose, strategy x2, reflect
    llm = MockLLMClient([
        decompose_resp,
        strategy_resp_1,
        strategy_resp_2,
        reflect_resp,
    ])

    decomposer = Decomposer(llm)
    selector = StrategySelector(llm)
    reflector = Reflector(llm)

    task_executor = MockTaskExecutor()
    event_bus = MockEventBus()
    subagent_executor = SubAgentExecutor(task_executor, event_bus)

    enhanced = EnhancedPlanningExecutor(
        decomposer=decomposer,
        strategy_selector=selector,
        reflector=reflector,
        subagent_executor=subagent_executor,
        max_iterations=3,
    )

    ctx = _make_worker_context()
    result = await enhanced.execute("Analyze sales data and write report", ctx)

    assert result.success_count == 2
    assert result.failure_count == 0
    assert len(task_executor.executed_agents) == 2

    # Verify EventBus received lifecycle events
    event_types = {e.type for e in event_bus.events}
    assert "subagent.started" in event_types
    assert "subagent.completed" in event_types


@pytest.mark.asyncio
async def test_planning_selected_skill_becomes_soft_preference():
    decompose_resp = _decomposition_response([
        {"id": "sg-1", "description": "Search data", "preferred_skill_ids": ["search", "analyze"]},
    ])
    strategy_resp_1 = _strategy_response("analyze")
    reflect_resp = _reflection_response(score=9)

    llm = MockLLMClient([
        decompose_resp,
        strategy_resp_1,
        reflect_resp,
    ])

    captured_contexts: list[SubAgentContext] = []

    class CapturingTaskExecutor(MockTaskExecutor):
        async def execute_subagent(self, context: SubAgentContext) -> str:
            captured_contexts.append(context)
            return await super().execute_subagent(context)

    decomposer = Decomposer(llm)
    selector = StrategySelector(llm)
    reflector = Reflector(llm)
    task_executor = CapturingTaskExecutor()
    subagent_executor = SubAgentExecutor(task_executor)
    enhanced = EnhancedPlanningExecutor(
        decomposer=decomposer,
        strategy_selector=selector,
        reflector=reflector,
        subagent_executor=subagent_executor,
        max_iterations=2,
    )

    await enhanced.execute("Search data", _make_worker_context())

    assert captured_contexts
    assert captured_contexts[0].skill_id is None
    assert captured_contexts[0].preferred_skill_ids == ("analyze", "search")


@pytest.mark.asyncio
async def test_planning_subagents_inherit_goal_default_pre_script():
    decompose_resp = _decomposition_response([
        {"id": "sg-1", "description": "Search data"},
    ])
    strategy_resp = _strategy_response("search")
    reflect_resp = _reflection_response(score=9)

    llm = MockLLMClient([
        decompose_resp,
        strategy_resp,
        reflect_resp,
    ])

    captured_contexts: list[SubAgentContext] = []

    class CapturingTaskExecutor(MockTaskExecutor):
        async def execute_subagent(self, context: SubAgentContext) -> str:
            captured_contexts.append(context)
            return await super().execute_subagent(context)

    enhanced = EnhancedPlanningExecutor(
        decomposer=Decomposer(llm),
        strategy_selector=StrategySelector(llm),
        reflector=Reflector(llm),
        subagent_executor=SubAgentExecutor(CapturingTaskExecutor()),
        max_iterations=2,
    )

    await enhanced.execute(
        "Search data",
        _make_worker_context(
            goal_default_pre_script=InlineScript(source="print('goal context')"),
        ),
    )

    assert captured_contexts
    assert isinstance(captured_contexts[0].pre_script, InlineScript)
    assert captured_contexts[0].pre_script.source.strip() == "print('goal context')"


@pytest.mark.asyncio
async def test_planning_loop_with_reflection_iteration():
    """When reflection score < 8, additional goals are added and re-executed."""
    decompose_resp = _decomposition_response([
        {"id": "sg-1", "description": "Collect data"},
    ])
    strategy_resp = _strategy_response("search")

    # First reflection: low score, add one more goal
    reflect_resp_1 = _reflection_response(
        score=5,
        missing=["validation"],
        additional=[{"id": "sg-extra-0", "description": "Validate data", "skill_hint": "validate"}],
    )
    # Strategy for extra goal
    strategy_resp_extra = _strategy_response("validate")
    # Second reflection: high score
    reflect_resp_2 = _reflection_response(score=9)

    llm = MockLLMClient([
        decompose_resp,       # decompose
        strategy_resp,        # strategy for sg-1
        reflect_resp_1,       # first reflect (low)
        strategy_resp_extra,  # strategy for sg-extra-0
        reflect_resp_2,       # second reflect (high)
    ])

    decomposer = Decomposer(llm)
    selector = StrategySelector(llm)
    reflector = Reflector(llm)

    task_executor = MockTaskExecutor()
    subagent_executor = SubAgentExecutor(task_executor)

    enhanced = EnhancedPlanningExecutor(
        decomposer=decomposer,
        strategy_selector=selector,
        reflector=reflector,
        subagent_executor=subagent_executor,
        max_iterations=3,
    )

    ctx = _make_worker_context()
    result = await enhanced.execute("Task needing iteration", ctx)

    # Should have executed both the original and additional goal
    assert result.success_count >= 2
    assert len(task_executor.executed_agents) >= 2


@pytest.mark.asyncio
async def test_planning_loop_max_iterations_limit():
    """Planning loop stops after max_iterations even if reflection says incomplete."""
    decompose_resp = _decomposition_response([
        {"id": "sg-1", "description": "Do work"},
    ])
    strategy_resp = _strategy_response("work")

    # Always low reflection with more goals
    def _low_reflect():
        return _reflection_response(
            score=3,
            missing=["more"],
            additional=[{"id": f"sg-extra", "description": "More work"}],
        )

    llm = MockLLMClient([
        decompose_resp,
        strategy_resp,
        _low_reflect(),
        strategy_resp,
        _low_reflect(),
        strategy_resp,
        _low_reflect(),
        strategy_resp,
        _low_reflect(),  # won't be reached if max=3
    ])

    decomposer = Decomposer(llm)
    selector = StrategySelector(llm)
    reflector = Reflector(llm)

    task_executor = MockTaskExecutor()
    subagent_executor = SubAgentExecutor(task_executor)

    enhanced = EnhancedPlanningExecutor(
        decomposer=decomposer,
        strategy_selector=selector,
        reflector=reflector,
        subagent_executor=subagent_executor,
        max_iterations=3,
    )

    ctx = _make_worker_context()
    result = await enhanced.execute("Never-ending task", ctx)

    # Should have run at most 3 iterations
    # Iteration 1: sg-1, iteration 2: sg-extra, iteration 3: another sg-extra
    assert len(task_executor.executed_agents) <= 4  # at most 3 iterations + 1 initial


@pytest.mark.asyncio
async def test_planning_with_parallel_goals():
    """Goals without dependencies execute in the same layer (parallel)."""
    decompose_resp = _decomposition_response([
        {"id": "sg-1", "description": "Task A"},
        {"id": "sg-2", "description": "Task B"},
        {"id": "sg-3", "description": "Merge", "depends_on": ["sg-1", "sg-2"]},
    ])
    strategy_resps = [
        _strategy_response("a"),
        _strategy_response("b"),
        _strategy_response("merge"),
    ]
    reflect_resp = _reflection_response(score=10)

    llm = MockLLMClient([decompose_resp] + strategy_resps + [reflect_resp])

    decomposer = Decomposer(llm)
    selector = StrategySelector(llm)
    reflector = Reflector(llm)

    task_executor = MockTaskExecutor()
    event_bus = MockEventBus()
    subagent_executor = SubAgentExecutor(task_executor, event_bus)

    enhanced = EnhancedPlanningExecutor(
        decomposer=decomposer,
        strategy_selector=selector,
        reflector=reflector,
        subagent_executor=subagent_executor,
    )

    ctx = _make_worker_context()
    result = await enhanced.execute("Parallel task", ctx)

    assert result.success_count == 3
    # All three agents were executed
    assert len(task_executor.executed_agents) == 3


@pytest.mark.asyncio
async def test_planning_subagent_memory_isolation():
    """SubAgent contexts receive read-only memory snapshots."""
    decompose_resp = _decomposition_response([
        {"id": "sg-1", "description": "Read data"},
    ])
    strategy_resp = _strategy_response("read")
    reflect_resp = _reflection_response(score=9)

    llm = MockLLMClient([decompose_resp, strategy_resp, reflect_resp])

    decomposer = Decomposer(llm)
    selector = StrategySelector(llm)
    reflector = Reflector(llm)

    captured_contexts: list[SubAgentContext] = []

    class CapturingExecutor:
        async def execute_subagent(self, context: SubAgentContext) -> str:
            captured_contexts.append(context)
            return "done"

    subagent_executor = SubAgentExecutor(CapturingExecutor())

    enhanced = EnhancedPlanningExecutor(
        decomposer=decomposer,
        strategy_selector=selector,
        reflector=reflector,
        subagent_executor=subagent_executor,
    )

    ctx = _make_worker_context()
    await enhanced.execute("Read task", ctx)

    assert len(captured_contexts) == 1
    sa_ctx = captured_contexts[0]
    # Memory and rules are empty tuples (read-only snapshots)
    assert isinstance(sa_ctx.memory_snapshot, tuple)
    assert isinstance(sa_ctx.rules_snapshot, tuple)
    # Cannot mutate
    with pytest.raises(AttributeError):
        sa_ctx.memory_snapshot = ()  # type: ignore[misc]


@pytest.mark.asyncio
async def test_planning_fail_fast_strategy():
    """EnhancedPlanningExecutor can use fail_fast collection strategy."""
    decompose_resp = _decomposition_response([
        {"id": "sg-1", "description": "Task A"},
        {"id": "sg-2", "description": "Task B"},
    ])
    strategy_resp = _strategy_response("work")
    reflect_resp = _reflection_response(score=9)

    llm = MockLLMClient([decompose_resp, strategy_resp, strategy_resp, reflect_resp])

    decomposer = Decomposer(llm)
    selector = StrategySelector(llm)
    reflector = Reflector(llm)

    task_executor = MockTaskExecutor()
    subagent_executor = SubAgentExecutor(task_executor)

    enhanced = EnhancedPlanningExecutor(
        decomposer=decomposer,
        strategy_selector=selector,
        reflector=reflector,
        subagent_executor=subagent_executor,
        collection_strategy="fail_fast",
    )

    ctx = _make_worker_context()
    result = await enhanced.execute("Two parallel tasks", ctx)

    assert result.success_count == 2


@pytest.mark.asyncio
async def test_planning_trace_reports_iteration_and_layer_results():
    """execute_with_trace returns structured layer traces without changing execute()."""
    decompose_resp = _decomposition_response([
        {"id": "sg-1", "description": "Task A"},
        {"id": "sg-2", "description": "Task B", "depends_on": ["sg-1"]},
    ])
    llm = MockLLMClient([
        decompose_resp,
        _strategy_response("work"),
        _strategy_response("work"),
        _reflection_response(score=9),
    ])
    enhanced = EnhancedPlanningExecutor(
        decomposer=Decomposer(llm),
        strategy_selector=StrategySelector(llm),
        reflector=Reflector(llm),
        subagent_executor=SubAgentExecutor(MockTaskExecutor()),
    )

    trace = await enhanced.execute_with_trace("Trace task", _make_worker_context())

    assert trace.aggregated_result.success_count == 2
    assert len(trace.iterations) == 1
    iteration = trace.iterations[0]
    assert iteration.iteration == 1
    assert len(iteration.layer_traces) == 2
    assert iteration.layer_traces[0].sub_goal_ids == ("sg-1",)
    assert iteration.layer_traces[1].sub_goal_ids == ("sg-2",)
