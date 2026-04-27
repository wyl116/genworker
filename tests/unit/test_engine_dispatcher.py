# edition: baseline
"""Tests for EngineDispatcher - routes to correct engine based on strategy.mode."""
import pytest

from src.engine.protocols import LLMResponse, ToolResult, UsageInfo
from src.engine.router.engine_dispatcher import EngineDispatcher
from src.engine.state import UsageBudget, WorkerContext
from src.skills.models import (
    FallbackConfig,
    RetryConfig,
    Skill,
    SkillStrategy,
    StrategyMode,
    WorkflowStep,
    WorkflowStepType,
)
from src.streaming.events import (
    ErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StepStartedEvent,
    TaskProgressEvent,
    TextMessageEvent,
)
from src.worker.planning.subagent.models import (
    AggregatedResult,
    SubAgentResult,
)


class MockLLM:
    def __init__(self, content="dispatched response"):
        self._content = content

    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
        return LLMResponse(
            content=self._content,
            usage=UsageInfo(total_tokens=5),
        )


class MockToolExecutor:
    async def execute(self, tool_name, tool_input):
        return ToolResult(content="tool result")


class MockPlanningExecutor:
    def __init__(self, result=None, error: Exception | None = None):
        self._result = result or AggregatedResult(
            sub_results=(
                SubAgentResult(
                    agent_id="sa-1",
                    sub_goal_id="goal-1",
                    status="success",
                    content="planned response",
                ),
            ),
            success_count=1,
            failure_count=0,
            combined_content="planned response",
        )
        self._error = error
        self.calls = []

    async def execute(self, task, worker_context):
        self.calls.append((task, worker_context.worker_id))
        if self._error is not None:
            raise self._error
        return self._result


class MockTracingPlanningExecutor(MockPlanningExecutor):
    async def execute_with_trace(
        self,
        task,
        worker_context,
        progress_callback=None,
    ):
        self.calls.append((task, worker_context.worker_id))
        if self._error is not None:
            raise self._error
        if progress_callback is not None:
            from src.worker.planning.enhanced_executor import (
                PlanningExecutionTrace,
                PlanningIterationTrace,
                PlanningLayerTrace,
            )

            layer_trace = PlanningLayerTrace(
                iteration=1,
                layer_index=1,
                sub_goal_ids=tuple(r.sub_goal_id for r in self._result.sub_results),
                results=self._result.sub_results,
            )
            await progress_callback(layer_trace)
            return PlanningExecutionTrace(
                aggregated_result=self._result,
                iterations=(
                    PlanningIterationTrace(
                        iteration=1,
                        layer_traces=(layer_trace,),
                        reflection=type(
                            "Reflection",
                            (),
                            {
                                "completeness_score": 9,
                                "missing_aspects": (),
                                "additional_sub_goals": (),
                            },
                        )(),
                        added_sub_goals=(),
                    ),
                ),
                total_sub_goals=len(self._result.sub_results),
                completed_sub_goals=len(self._result.sub_results),
            )
        return await self.execute(task, worker_context)


class MockLangGraphEngine:
    def __init__(self, *, error: Exception | None = None):
        self._error = error
        self.calls = []

    async def execute(
        self,
        skill,
        worker_context,
        task,
        *,
        available_tools=None,
        budget=None,
        run_id,
        checkpoint_handle=None,
    ):
        self.calls.append((skill.skill_id, worker_context.worker_id, task, run_id))
        if self._error is not None:
            raise self._error
        yield TextMessageEvent(run_id=run_id, content="langgraph response")
        yield RunFinishedEvent(run_id=run_id, success=True)


def _context():
    return WorkerContext(
        worker_id="w1",
        tenant_id="t1",
        identity="Test worker",
    )


def _skill(mode, workflow=(), fallback=None):
    return Skill(
        skill_id="test",
        name="Test",
        strategy=SkillStrategy(mode=mode, workflow=workflow, fallback=fallback),
        instructions={"general": "Do the task."},
        keywords=(),
    )


async def _collect_events(dispatcher, skill, task="test task"):
    events = []
    async for e in dispatcher.dispatch(
        skill=skill,
        worker_context=_context(),
        task=task,
    ):
        events.append(e)
    return events


@pytest.mark.asyncio
async def test_dispatches_autonomous():
    """Autonomous mode routes to ReactEngine."""
    dispatcher = EngineDispatcher(MockLLM(), MockToolExecutor())
    skill = _skill(StrategyMode.AUTONOMOUS)
    events = await _collect_events(dispatcher, skill)

    assert isinstance(events[0], RunStartedEvent)
    assert isinstance(events[-1], RunFinishedEvent)
    text_events = [e for e in events if isinstance(e, TextMessageEvent)]
    assert len(text_events) > 0
    assert text_events[0].content == "dispatched response"


@pytest.mark.asyncio
async def test_dispatches_deterministic():
    """Deterministic mode routes to WorkflowEngine with steps."""
    dispatcher = EngineDispatcher(MockLLM(), MockToolExecutor())
    steps = (
        WorkflowStep(
            step="planning", type=WorkflowStepType.DETERMINISTIC,
            instruction_ref="general",
        ),
        WorkflowStep(
            step="execution", type=WorkflowStepType.DETERMINISTIC,
            instruction_ref="general",
        ),
    )
    skill = _skill(StrategyMode.DETERMINISTIC, workflow=steps)
    events = await _collect_events(dispatcher, skill)

    step_names = [e.step_name for e in events if isinstance(e, StepStartedEvent)]
    assert step_names == ["planning", "execution"]
    assert isinstance(events[-1], RunFinishedEvent)


@pytest.mark.asyncio
async def test_dispatches_hybrid():
    """Hybrid mode routes to HybridEngine."""
    dispatcher = EngineDispatcher(MockLLM(), MockToolExecutor())
    steps = (
        WorkflowStep(
            step="plan", type=WorkflowStepType.AUTONOMOUS,
            max_rounds=1,
        ),
        WorkflowStep(
            step="exec", type=WorkflowStepType.DETERMINISTIC,
            instruction_ref="general",
        ),
    )
    skill = _skill(StrategyMode.HYBRID, workflow=steps)
    events = await _collect_events(dispatcher, skill)

    step_names = [e.step_name for e in events if isinstance(e, StepStartedEvent)]
    assert step_names == ["plan", "exec"]
    assert isinstance(events[-1], RunFinishedEvent)


@pytest.mark.asyncio
async def test_fallback_degrades_to_autonomous():
    """When fallback condition is met, degrade to autonomous."""
    dispatcher = EngineDispatcher(MockLLM(), MockToolExecutor())
    skill = _skill(
        StrategyMode.HYBRID,
        workflow=(),  # empty workflow triggers fallback
        fallback=FallbackConfig(condition="empty_steps", mode=StrategyMode.AUTONOMOUS),
    )
    events = await _collect_events(dispatcher, skill)

    # Should run as autonomous (no StepStartedEvent)
    step_events = [e for e in events if isinstance(e, StepStartedEvent)]
    assert len(step_events) == 0
    assert isinstance(events[0], RunStartedEvent)
    assert isinstance(events[-1], RunFinishedEvent)


@pytest.mark.asyncio
async def test_no_fallback_when_condition_not_met():
    """Fallback not triggered when condition is not met."""
    dispatcher = EngineDispatcher(MockLLM(), MockToolExecutor())
    steps = (
        WorkflowStep(step="s1", type=WorkflowStepType.DETERMINISTIC),
    )
    skill = _skill(
        StrategyMode.DETERMINISTIC,
        workflow=steps,
        fallback=FallbackConfig(condition="empty_steps", mode=StrategyMode.AUTONOMOUS),
    )
    events = await _collect_events(dispatcher, skill)

    # Should still run deterministic (has steps)
    step_events = [e for e in events if isinstance(e, StepStartedEvent)]
    assert len(step_events) == 1


@pytest.mark.asyncio
async def test_dispatches_planning():
    """Planning mode routes to EnhancedPlanningExecutor."""
    planning = MockPlanningExecutor()
    dispatcher = EngineDispatcher(
        MockLLM(),
        MockToolExecutor(),
        enhanced_planning_executor=planning,
    )
    skill = _skill(StrategyMode.PLANNING)
    events = await _collect_events(dispatcher, skill, task="plan this task")

    assert isinstance(events[0], RunStartedEvent)
    step_names = [e.step_name for e in events if isinstance(e, StepStartedEvent)]
    assert step_names == ["planning"]
    progress_events = [e for e in events if isinstance(e, TaskProgressEvent)]
    assert len(progress_events) == 1
    assert progress_events[0].task_id == "goal-1"


@pytest.mark.asyncio
async def test_dispatches_langgraph():
    engine = MockLangGraphEngine()
    dispatcher = EngineDispatcher(
        MockLLM(),
        MockToolExecutor(),
        langgraph_engine=engine,
    )
    skill = _skill(StrategyMode.LANGGRAPH)

    events = await _collect_events(dispatcher, skill, task="graph task")

    text_events = [e for e in events if isinstance(e, TextMessageEvent)]
    assert text_events
    assert text_events[0].content == "langgraph response"
    assert engine.calls


@pytest.mark.asyncio
async def test_langgraph_fallback_to_autonomous_on_engine_error():
    dispatcher = EngineDispatcher(
        MockLLM(),
        MockToolExecutor(),
        langgraph_engine=MockLangGraphEngine(error=RuntimeError("graph unavailable")),
    )
    skill = _skill(
        StrategyMode.LANGGRAPH,
        fallback=FallbackConfig(condition="langgraph_unavailable", mode="autonomous"),
    )

    events = await _collect_events(dispatcher, skill, task="graph task")

    text_events = [e for e in events if isinstance(e, TextMessageEvent)]
    assert text_events
    assert text_events[0].content == "dispatched response"
    assert isinstance(events[-1], RunFinishedEvent)


@pytest.mark.asyncio
async def test_langgraph_fallback_accepts_enum_autonomous_mode():
    dispatcher = EngineDispatcher(
        MockLLM(),
        MockToolExecutor(),
        langgraph_engine=MockLangGraphEngine(error=RuntimeError("graph unavailable")),
    )
    skill = _skill(
        StrategyMode.LANGGRAPH,
        fallback=FallbackConfig(
            condition="langgraph_unavailable",
            mode=StrategyMode.AUTONOMOUS,
        ),
    )

    events = await _collect_events(dispatcher, skill, task="graph task")

    text_events = [e for e in events if isinstance(e, TextMessageEvent)]
    assert text_events
    assert text_events[0].content == "dispatched response"
    assert isinstance(events[-1], RunFinishedEvent)


@pytest.mark.asyncio
async def test_planning_emits_progress_for_each_sub_goal():
    """Planning results are expanded into per-sub-goal progress events."""
    planning = MockPlanningExecutor(result=AggregatedResult(
        sub_results=(
            SubAgentResult(
                agent_id="sa-1",
                sub_goal_id="goal-a",
                status="success",
                content="alpha",
            ),
            SubAgentResult(
                agent_id="sa-2",
                sub_goal_id="goal-b",
                status="failure",
                content="",
                error="beta failed",
            ),
        ),
        success_count=1,
        failure_count=1,
        combined_content="alpha",
    ))
    dispatcher = EngineDispatcher(
        MockLLM(),
        MockToolExecutor(),
        enhanced_planning_executor=planning,
    )
    events = await _collect_events(dispatcher, _skill(StrategyMode.PLANNING))

    progress_events = [e for e in events if isinstance(e, TaskProgressEvent)]
    assert [e.task_id for e in progress_events] == ["goal-a", "goal-b"]
    assert [e.progress for e in progress_events] == [0.5, 1.0]
    assert progress_events[1].current_step == "goal-b (failure)"


@pytest.mark.asyncio
async def test_planning_trace_callback_emits_layer_progress_immediately():
    """Tracing executors emit iteration/layer-aware progress events."""
    dispatcher = EngineDispatcher(
        MockLLM(),
        MockToolExecutor(),
        enhanced_planning_executor=MockTracingPlanningExecutor(),
    )
    events = await _collect_events(dispatcher, _skill(StrategyMode.PLANNING))

    progress_events = [e for e in events if isinstance(e, TaskProgressEvent)]
    assert len(progress_events) == 1
    assert progress_events[0].current_step == "iteration 1, layer 1: goal-1 (success)"


@pytest.mark.asyncio
async def test_planning_executor_error_surfaces_as_stream_error():
    """Planning exceptions are converted into ErrorEvent and failed completion."""
    dispatcher = EngineDispatcher(
        MockLLM(),
        MockToolExecutor(),
        enhanced_planning_executor=MockPlanningExecutor(
            error=RuntimeError("planner exploded")
        ),
    )
    skill = _skill(StrategyMode.PLANNING)
    events = await _collect_events(dispatcher, skill)

    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(error_events) == 1
    assert "planner exploded" in error_events[0].message
    assert isinstance(events[-1], RunFinishedEvent)
    assert events[-1].success is False
