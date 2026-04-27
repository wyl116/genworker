# edition: baseline
"""Tests for SubAgent dispatch tool - Coordinator pattern integration."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.events.models import Event
from src.tools.formatters import ToolResult
from src.worker.planning.models import SubGoal
from src.worker.planning.subagent.executor import SubAgentExecutor
from src.worker.planning.subagent.models import (
    AggregatedResult,
    SubAgentContext,
    SubAgentResult,
    SubAgentUsage,
)
from src.engine.tools.subagent_tool import (
    MAX_SUBTASKS,
    SubTaskSpec,
    create_spawn_subagents_tool,
    execute_spawn_subagents,
    format_results_for_llm,
    _parse_subtasks,
    _specs_to_subgoals,
    _build_contexts,
)


# ---------------------------------------------------------------------------
# Mock implementations
# ---------------------------------------------------------------------------

class MockTaskExecutor:
    """Simulates SubAgent task execution."""

    def __init__(
        self,
        results: dict[str, str] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self._results = results or {}
        self._errors = errors or {}
        self.executed: list[str] = []
        self.started: dict[str, asyncio.Event] = {}

    async def execute_subagent(self, context: SubAgentContext) -> str:
        self.executed.append(context.agent_id)
        self.started.setdefault(context.agent_id, asyncio.Event()).set()
        error = self._errors.get(context.agent_id)
        if error is not None:
            raise error
        return self._results.get(context.agent_id, f"result-{context.agent_id}")


class MockEventBus:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> int:
        self.events.append(event)
        return 1


# ---------------------------------------------------------------------------
# Test _parse_subtasks
# ---------------------------------------------------------------------------

class TestParseSubtasks:
    def test_valid_subtasks(self):
        raw = [
            {"id": "t1", "description": "Research APIs"},
            {"id": "t2", "description": "Analyze results", "depends_on": ["t1"]},
        ]
        specs = _parse_subtasks(raw)
        assert len(specs) == 2
        assert specs[0].id == "t1"
        assert specs[0].description == "Research APIs"
        assert specs[1].depends_on == ("t1",)

    def test_empty_description_skipped(self):
        raw = [
            {"id": "t1", "description": ""},
            {"id": "t2", "description": "Valid task"},
        ]
        specs = _parse_subtasks(raw)
        assert len(specs) == 1
        assert specs[0].id == "t2"

    def test_max_subtasks_enforced(self):
        raw = [{"id": f"t{i}", "description": f"Task {i}"} for i in range(10)]
        specs = _parse_subtasks(raw)
        assert len(specs) == MAX_SUBTASKS

    def test_default_id_generation(self):
        raw = [{"description": "No explicit ID"}]
        specs = _parse_subtasks(raw)
        assert specs[0].id == "subtask-0"

    def test_skill_hint_preserved(self):
        raw = [{"id": "t1", "description": "Task", "skill_hint": "analysis"}]
        specs = _parse_subtasks(raw)
        assert specs[0].skill_hint == "analysis"

    def test_preferred_skill_ids_preserved(self):
        raw = [{"id": "t1", "description": "Task", "preferred_skill_ids": ["analysis", "report"]}]
        specs = _parse_subtasks(raw)
        assert specs[0].preferred_skill_ids == ("analysis", "report")


# ---------------------------------------------------------------------------
# Test _specs_to_subgoals
# ---------------------------------------------------------------------------

class TestSpecsToSubgoals:
    def test_conversion(self):
        specs = (
            SubTaskSpec(id="t1", description="First", depends_on=()),
            SubTaskSpec(id="t2", description="Second", depends_on=("t1",)),
        )
        goals = _specs_to_subgoals(specs)
        assert len(goals) == 2
        assert isinstance(goals[0], SubGoal)
        assert goals[0].id == "t1"
        assert goals[1].depends_on == ("t1",)

    def test_empty_input(self):
        goals = _specs_to_subgoals(())
        assert goals == ()


# ---------------------------------------------------------------------------
# Test _build_contexts
# ---------------------------------------------------------------------------

class TestBuildContexts:
    def test_context_fields(self):
        goals = (SubGoal(id="g1", description="Goal 1"),)
        contexts = _build_contexts(
            sub_goals=goals,
            worker_id="worker-1",
            parent_task_id="task-1",
            tool_sandbox=("bash_tool", "read_file"),
            timeout=60,
            max_rounds=5,
        )
        assert len(contexts) == 1
        ctx = contexts[0]
        assert ctx.agent_id == "sa-worker-1-g1"
        assert ctx.parent_worker_id == "worker-1"
        assert ctx.parent_task_id == "task-1"
        assert ctx.tool_sandbox == ("bash_tool", "read_file")
        assert ctx.timeout_seconds == 60
        assert ctx.max_rounds == 5

    def test_context_uses_soft_preferred_skills(self):
        goals = (
            SubGoal(
                id="g1",
                description="Goal 1",
                preferred_skill_ids=("analysis", "report"),
            ),
        )
        contexts = _build_contexts(
            sub_goals=goals,
            worker_id="worker-1",
            parent_task_id="task-1",
            tool_sandbox=("bash_tool",),
            timeout=60,
            max_rounds=5,
        )
        assert contexts[0].skill_id is None
        assert contexts[0].preferred_skill_ids == ("analysis", "report")


# ---------------------------------------------------------------------------
# Test format_results_for_llm
# ---------------------------------------------------------------------------

class TestFormatResultsForLLM:
    def test_success_formatting(self):
        agg = AggregatedResult(
            sub_results=(
                SubAgentResult(
                    agent_id="sa-1",
                    sub_goal_id="g1",
                    status="success",
                    content="Found 3 API endpoints.",
                ),
            ),
            success_count=1,
            failure_count=0,
            combined_content="Found 3 API endpoints.",
        )
        text = format_results_for_llm(agg)
        assert "Completed: 1" in text
        assert "Failed: 0" in text
        assert "[OK] g1" in text
        assert "Found 3 API endpoints." in text

    def test_failure_formatting(self):
        agg = AggregatedResult(
            sub_results=(
                SubAgentResult(
                    agent_id="sa-1",
                    sub_goal_id="g1",
                    status="failure",
                    content="",
                    error="Timeout after 120s",
                ),
            ),
            success_count=0,
            failure_count=1,
            combined_content="",
        )
        text = format_results_for_llm(agg)
        assert "[FAIL] g1" in text
        assert "Timeout after 120s" in text

    def test_mixed_results(self):
        agg = AggregatedResult(
            sub_results=(
                SubAgentResult(
                    agent_id="sa-1", sub_goal_id="g1",
                    status="success", content="OK",
                ),
                SubAgentResult(
                    agent_id="sa-2", sub_goal_id="g2",
                    status="failure", content="", error="Error",
                ),
            ),
            success_count=1,
            failure_count=1,
            combined_content="OK",
        )
        text = format_results_for_llm(agg)
        assert "Completed: 1" in text
        assert "Failed: 1" in text
        assert "Total: 2" in text


# ---------------------------------------------------------------------------
# Test execute_spawn_subagents (integration with SubAgentExecutor)
# ---------------------------------------------------------------------------

class TestExecuteSpawnSubagents:
    @pytest.mark.asyncio
    async def test_successful_execution(self):
        mock_executor_impl = MockTaskExecutor(results={
            "sa-w1-t1": "Result from task 1",
            "sa-w1-t2": "Result from task 2",
        })
        executor = SubAgentExecutor(
            task_executor=mock_executor_impl,
            max_concurrent_subagents=3,
        )

        tool_input = {
            "subtasks": [
                {"id": "t1", "description": "Research APIs"},
                {"id": "t2", "description": "Analyze logs"},
            ],
            "strategy": "best_effort",
        }

        result = await execute_spawn_subagents(
            tool_input=tool_input,
            executor=executor,
            worker_id="w1",
            parent_task_id="task-main",
            tool_sandbox=("bash_tool",),
        )

        assert not result.is_error
        assert "Completed: 2" in result.content
        assert "Result from task 1" in result.content
        assert "Result from task 2" in result.content

    @pytest.mark.asyncio
    async def test_empty_subtasks_returns_error(self):
        executor = SubAgentExecutor(
            task_executor=MockTaskExecutor(),
        )

        result = await execute_spawn_subagents(
            tool_input={"subtasks": []},
            executor=executor,
            worker_id="w1",
            parent_task_id="task-1",
            tool_sandbox=(),
        )

        assert result.is_error
        assert "required" in result.content.lower()

    @pytest.mark.asyncio
    async def test_missing_subtasks_returns_error(self):
        executor = SubAgentExecutor(
            task_executor=MockTaskExecutor(),
        )

        result = await execute_spawn_subagents(
            tool_input={},
            executor=executor,
            worker_id="w1",
            parent_task_id="task-1",
            tool_sandbox=(),
        )

        assert result.is_error

    @pytest.mark.asyncio
    async def test_partial_failure_best_effort(self):
        mock_executor_impl = MockTaskExecutor(
            results={"sa-w1-t1": "Success"},
            errors={"sa-w1-t2": RuntimeError("Connection refused")},
        )
        executor = SubAgentExecutor(
            task_executor=mock_executor_impl,
            max_concurrent_subagents=3,
        )

        tool_input = {
            "subtasks": [
                {"id": "t1", "description": "Task 1"},
                {"id": "t2", "description": "Task 2"},
            ],
            "strategy": "best_effort",
        }

        result = await execute_spawn_subagents(
            tool_input=tool_input,
            executor=executor,
            worker_id="w1",
            parent_task_id="task-1",
            tool_sandbox=(),
        )

        assert not result.is_error
        assert "Completed: 1" in result.content
        assert "Failed: 1" in result.content
        assert "[OK] t1" in result.content
        assert "[FAIL] t2" in result.content

    @pytest.mark.asyncio
    async def test_fail_fast_strategy(self):
        mock_executor_impl = MockTaskExecutor(
            errors={"sa-w1-t1": RuntimeError("Fail")},
            results={"sa-w1-t2": "Should not run"},
        )
        executor = SubAgentExecutor(
            task_executor=mock_executor_impl,
            max_concurrent_subagents=3,
        )

        tool_input = {
            "subtasks": [
                {"id": "t1", "description": "Failing task"},
                {"id": "t2", "description": "Second task"},
            ],
            "strategy": "fail_fast",
        }

        result = await execute_spawn_subagents(
            tool_input=tool_input,
            executor=executor,
            worker_id="w1",
            parent_task_id="task-1",
            tool_sandbox=(),
        )

        assert not result.is_error
        assert "Failed:" in result.content

    @pytest.mark.asyncio
    async def test_depends_on_runs_in_topological_order(self):
        first_completed = asyncio.Event()

        class OrderedExecutor(MockTaskExecutor):
            async def execute_subagent(self, context: SubAgentContext) -> str:
                if context.sub_goal.id == "t1":
                    await asyncio.sleep(0.05)
                    self.executed.append(context.agent_id)
                    first_completed.set()
                    return "first done"
                assert first_completed.is_set()
                self.executed.append(context.agent_id)
                return "second done"

        executor = SubAgentExecutor(
            task_executor=OrderedExecutor(),
            max_concurrent_subagents=3,
        )

        result = await execute_spawn_subagents(
            tool_input={
                "subtasks": [
                    {"id": "t1", "description": "First"},
                    {"id": "t2", "description": "Second", "depends_on": ["t1"]},
                ],
                "strategy": "best_effort",
            },
            executor=executor,
            worker_id="w1",
            parent_task_id="task-main",
            tool_sandbox=(),
        )

        assert not result.is_error
        assert "first done" in result.content
        assert "second done" in result.content
        assert executor._task_executor.executed == ["sa-w1-t1", "sa-w1-t2"]


# ---------------------------------------------------------------------------
# Test create_spawn_subagents_tool
# ---------------------------------------------------------------------------

class TestCreateSpawnSubagentsTool:
    def test_tool_creation(self):
        mock_executor_impl = MockTaskExecutor()
        executor = SubAgentExecutor(task_executor=mock_executor_impl)

        tool = create_spawn_subagents_tool(
            executor=executor,
            worker_id="w1",
            parent_task_id="task-1",
            tool_sandbox=("bash_tool",),
        )

        assert tool.name == "spawn_subagents"
        assert tool.enabled is True
        assert "subagent" in tool.tags

    def test_tool_openai_schema(self):
        mock_executor_impl = MockTaskExecutor()
        executor = SubAgentExecutor(task_executor=mock_executor_impl)

        tool = create_spawn_subagents_tool(
            executor=executor,
            worker_id="w1",
            parent_task_id="task-1",
            tool_sandbox=(),
        )

        schema = tool.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "spawn_subagents"
        assert "subtasks" in schema["function"]["parameters"]["properties"]
        assert "subtasks" in schema["function"]["parameters"]["required"]

    @pytest.mark.asyncio
    async def test_tool_handler_invocation(self):
        mock_executor_impl = MockTaskExecutor(
            results={"sa-w1-t1": "Handler result"},
        )
        executor = SubAgentExecutor(task_executor=mock_executor_impl)

        tool = create_spawn_subagents_tool(
            executor=executor,
            worker_id="w1",
            parent_task_id="task-1",
            tool_sandbox=(),
        )

        result = await tool.handler(
            subtasks=[{"id": "t1", "description": "Test task"}],
        )

        assert isinstance(result, ToolResult)
        assert not result.is_error
        assert "Handler result" in result.content
