# edition: baseline
"""Tests for the planning Decomposer."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from src.engine.protocols import LLMResponse
from src.services.llm.intent import Purpose
from src.worker.planning.decomposer import Decomposer
from src.worker.planning.models import PlanningError, PlanningResult, SubGoal


# ---------------------------------------------------------------------------
# Mock LLM client
# ---------------------------------------------------------------------------

class MockLLMClient:
    """LLM client that returns pre-configured responses."""

    def __init__(self, response_content: str = "") -> None:
        self._response_content = response_content
        self.call_count = 0
        self.last_messages: list[dict[str, Any]] = []
        self.last_intent = None

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        intent=None,
    ) -> LLMResponse:
        self.call_count += 1
        self.last_messages = messages
        self.last_intent = intent
        return LLMResponse(content=self._response_content)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_decomposition_json(
    sub_goals: list[dict[str, Any]],
    reasoning: str = "test reasoning",
) -> str:
    return json.dumps({
        "sub_goals": sub_goals,
        "reasoning": reasoning,
    })


# ---------------------------------------------------------------------------
# Tests: successful decomposition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_decompose_single_goal():
    """A simple task produces at least one SubGoal."""
    response = _make_decomposition_json([
        {"id": "sg-1", "description": "Search for data", "skill_hint": "search"},
    ])
    llm = MockLLMClient(response)
    decomposer = Decomposer(llm)

    result = await decomposer.decompose("Find recent sales data")

    assert isinstance(result, PlanningResult)
    assert len(result.sub_goals) == 1
    assert result.sub_goals[0].id == "sg-1"
    assert result.sub_goals[0].skill_hint == "search"
    assert result.execution_order == ("sg-1",)
    assert result.reasoning == "test reasoning"


@pytest.mark.asyncio
async def test_decompose_parses_preferred_skill_ids():
    response = _make_decomposition_json([
        {
            "id": "sg-1",
            "description": "Search for data",
            "preferred_skill_ids": ["search", "analyze"],
        },
    ])
    llm = MockLLMClient(response)
    decomposer = Decomposer(llm)

    result = await decomposer.decompose("Find recent sales data")

    assert result.sub_goals[0].preferred_skill_ids == ("search", "analyze")
    assert result.sub_goals[0].soft_preferred_skill_ids == ("search", "analyze")


@pytest.mark.asyncio
async def test_decompose_parses_skills_alias():
    response = _make_decomposition_json([
        {
            "id": "sg-1",
            "description": "Search for data",
            "skills": ["search", "analyze"],
        },
    ])
    llm = MockLLMClient(response)
    decomposer = Decomposer(llm)

    result = await decomposer.decompose("Find recent sales data")

    assert result.sub_goals[0].preferred_skill_ids == ("search", "analyze")


@pytest.mark.asyncio
async def test_decompose_multiple_goals_with_dependencies():
    """Multiple sub-goals with dependencies produce correct topological order."""
    response = _make_decomposition_json([
        {"id": "sg-1", "description": "Collect data", "skill_hint": "search"},
        {"id": "sg-2", "description": "Analyze data", "skill_hint": "analyze", "depends_on": ["sg-1"]},
        {"id": "sg-3", "description": "Write report", "skill_hint": "write", "depends_on": ["sg-2"]},
    ])
    llm = MockLLMClient(response)
    decomposer = Decomposer(llm)

    result = await decomposer.decompose("Analyze sales and write report")

    assert len(result.sub_goals) == 3
    # sg-1 must come before sg-2, sg-2 before sg-3
    order = list(result.execution_order)
    assert order.index("sg-1") < order.index("sg-2")
    assert order.index("sg-2") < order.index("sg-3")


@pytest.mark.asyncio
async def test_decompose_parallel_goals():
    """Independent goals can appear in the same layer."""
    response = _make_decomposition_json([
        {"id": "sg-1", "description": "Task A"},
        {"id": "sg-2", "description": "Task B"},
        {"id": "sg-3", "description": "Merge", "depends_on": ["sg-1", "sg-2"]},
    ])
    llm = MockLLMClient(response)
    decomposer = Decomposer(llm)

    result = await decomposer.decompose("Do A and B then merge")

    # sg-1 and sg-2 are independent, both before sg-3
    order = list(result.execution_order)
    assert order.index("sg-1") < order.index("sg-3")
    assert order.index("sg-2") < order.index("sg-3")


@pytest.mark.asyncio
async def test_decompose_with_markdown_fences():
    """LLM response wrapped in ```json fences is parsed correctly."""
    inner = _make_decomposition_json([
        {"id": "sg-1", "description": "Do something"},
    ])
    response = f"```json\n{inner}\n```"
    llm = MockLLMClient(response)
    decomposer = Decomposer(llm)

    result = await decomposer.decompose("task")

    assert len(result.sub_goals) == 1


@pytest.mark.asyncio
async def test_decompose_auto_generates_ids():
    """SubGoals without explicit IDs get auto-generated ones."""
    response = _make_decomposition_json([
        {"description": "Step one"},
        {"description": "Step two"},
    ])
    llm = MockLLMClient(response)
    decomposer = Decomposer(llm)

    result = await decomposer.decompose("task")

    assert result.sub_goals[0].id == "sg-0"
    assert result.sub_goals[1].id == "sg-1"


# ---------------------------------------------------------------------------
# Tests: error cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_decompose_empty_goals_raises():
    """Empty sub_goals list raises PlanningError."""
    response = json.dumps({"sub_goals": [], "reasoning": ""})
    llm = MockLLMClient(response)
    decomposer = Decomposer(llm)

    with pytest.raises(PlanningError, match="no sub-goals"):
        await decomposer.decompose("task")


@pytest.mark.asyncio
async def test_decompose_invalid_json_raises():
    """Non-JSON response raises PlanningError."""
    llm = MockLLMClient("this is not json")
    decomposer = Decomposer(llm)

    with pytest.raises(PlanningError, match="Failed to parse"):
        await decomposer.decompose("task")


@pytest.mark.asyncio
async def test_decompose_cyclic_dependency_raises():
    """Cyclic dependencies raise PlanningError."""
    response = _make_decomposition_json([
        {"id": "sg-1", "description": "A", "depends_on": ["sg-2"]},
        {"id": "sg-2", "description": "B", "depends_on": ["sg-1"]},
    ])
    llm = MockLLMClient(response)
    decomposer = Decomposer(llm)

    with pytest.raises(PlanningError, match="循环"):
        await decomposer.decompose("task")


@pytest.mark.asyncio
async def test_decompose_passes_context_to_llm():
    """Decomposer passes worker context into the LLM prompt."""
    response = _make_decomposition_json([
        {"id": "sg-1", "description": "Do thing"},
    ])
    llm = MockLLMClient(response)
    decomposer = Decomposer(llm)

    await decomposer.decompose(
        task="my task",
        worker_name="TestWorker",
        worker_role="analyst",
        available_skills="search, analyze",
    )

    assert llm.call_count == 1
    prompt_text = llm.last_messages[0]["content"]
    assert "TestWorker" in prompt_text
    assert "my task" in prompt_text
    assert "search, analyze" in prompt_text
    assert llm.last_intent.purpose is Purpose.PLAN


# ---------------------------------------------------------------------------
# Tests: frozen dataclass properties
# ---------------------------------------------------------------------------

def test_subgoal_is_frozen():
    """SubGoal is immutable."""
    sg = SubGoal(id="sg-1", description="test")
    with pytest.raises(AttributeError):
        sg.id = "changed"  # type: ignore[misc]


def test_planning_result_is_frozen():
    """PlanningResult is immutable."""
    pr = PlanningResult(
        sub_goals=(SubGoal(id="sg-1", description="test"),),
        execution_order=("sg-1",),
        reasoning="test",
    )
    with pytest.raises(AttributeError):
        pr.reasoning = "changed"  # type: ignore[misc]
