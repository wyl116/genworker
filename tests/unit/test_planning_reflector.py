# edition: baseline
"""Tests for the planning Reflector."""
from __future__ import annotations

import json
from typing import Any

import pytest

from src.engine.protocols import LLMResponse
from src.services.llm.intent import Purpose
from src.worker.planning.models import PlanningError, ReflectionResult, SubGoal
from src.worker.planning.reflector import COMPLETENESS_THRESHOLD, Reflector


# ---------------------------------------------------------------------------
# Mock LLM client
# ---------------------------------------------------------------------------

class MockLLMClient:
    """LLM client returning pre-configured responses."""

    def __init__(self, response_content: str = "") -> None:
        self._response_content = response_content
        self.call_count = 0
        self.last_intent = None

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        intent=None,
    ) -> LLMResponse:
        self.call_count += 1
        self.last_intent = intent
        return LLMResponse(content=self._response_content)


def _make_reflection_json(
    score: int = 9,
    missing: list[str] | None = None,
    additional: list[dict[str, Any]] | None = None,
) -> str:
    return json.dumps({
        "completeness_score": score,
        "missing_aspects": missing or [],
        "additional_sub_goals": additional or [],
    })


# ---------------------------------------------------------------------------
# Tests: successful reflection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reflect_high_score():
    """Score >= threshold means no iteration needed."""
    response = _make_reflection_json(score=9)
    reflector = Reflector(MockLLMClient(response))

    result = await reflector.reflect("original task", "results text")

    assert isinstance(result, ReflectionResult)
    assert result.completeness_score == 9
    assert result.missing_aspects == ()
    assert result.additional_sub_goals == ()
    assert not reflector.needs_iteration(result)


@pytest.mark.asyncio
async def test_reflect_low_score_triggers_iteration():
    """Score < threshold triggers iteration with additional sub-goals."""
    response = _make_reflection_json(
        score=5,
        missing=["data validation", "edge cases"],
        additional=[
            {"id": "sg-extra-0", "description": "Validate data"},
            {"id": "sg-extra-1", "description": "Handle edge cases"},
        ],
    )
    reflector = Reflector(MockLLMClient(response))

    result = await reflector.reflect("original task", "partial results")

    assert result.completeness_score == 5
    assert len(result.missing_aspects) == 2
    assert len(result.additional_sub_goals) == 2
    assert reflector.needs_iteration(result)


@pytest.mark.asyncio
async def test_reflect_additional_goals_parse_preferred_skill_ids():
    response = _make_reflection_json(
        score=5,
        additional=[
            {
                "id": "sg-extra-0",
                "description": "Validate data",
                "preferred_skill_ids": ["validate", "analyze"],
            },
        ],
    )
    reflector = Reflector(MockLLMClient(response))

    result = await reflector.reflect("original task", "partial results")

    assert result.additional_sub_goals[0].preferred_skill_ids == ("validate", "analyze")


@pytest.mark.asyncio
async def test_reflect_boundary_score():
    """Score exactly at threshold does not trigger iteration."""
    response = _make_reflection_json(score=COMPLETENESS_THRESHOLD)
    reflector = Reflector(MockLLMClient(response))

    result = await reflector.reflect("task", "results")

    assert not reflector.needs_iteration(result)


@pytest.mark.asyncio
async def test_reflect_score_just_below_threshold():
    """Score one below threshold triggers iteration."""
    response = _make_reflection_json(score=COMPLETENESS_THRESHOLD - 1)
    reflector = Reflector(MockLLMClient(response))

    result = await reflector.reflect("task", "results")

    assert reflector.needs_iteration(result)


@pytest.mark.asyncio
async def test_reflect_custom_threshold():
    """Custom threshold is respected."""
    response = _make_reflection_json(score=5)
    reflector = Reflector(MockLLMClient(response), threshold=3)

    result = await reflector.reflect("task", "results")

    assert not reflector.needs_iteration(result)


@pytest.mark.asyncio
async def test_reflect_additional_goals_as_strings():
    """Additional sub-goals given as plain strings are converted to SubGoals."""
    response = _make_reflection_json(
        score=4,
        additional=[
            {"description": "Extra step A"},
            {"description": "Extra step B"},
        ],
    )
    reflector = Reflector(MockLLMClient(response))

    result = await reflector.reflect("task", "results")

    assert len(result.additional_sub_goals) == 2
    for sg in result.additional_sub_goals:
        assert isinstance(sg, SubGoal)
        assert sg.description


@pytest.mark.asyncio
async def test_reflect_score_clamped_to_range():
    """Scores outside 0-10 are clamped."""
    response = _make_reflection_json(score=15)
    reflector = Reflector(MockLLMClient(response))

    result = await reflector.reflect("task", "results")

    assert result.completeness_score == 10

    response_low = _make_reflection_json(score=-3)
    reflector_low = Reflector(MockLLMClient(response_low))

    result_low = await reflector_low.reflect("task", "results")

    assert result_low.completeness_score == 0


@pytest.mark.asyncio
async def test_reflect_with_markdown_fences():
    """JSON wrapped in markdown code fences is parsed correctly."""
    inner = _make_reflection_json(score=7, missing=["one thing"])
    response = f"```json\n{inner}\n```"
    reflector = Reflector(MockLLMClient(response))

    result = await reflector.reflect("task", "results")

    assert result.completeness_score == 7
    assert result.missing_aspects == ("one thing",)


# ---------------------------------------------------------------------------
# Tests: error cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reflect_invalid_json_raises():
    """Non-JSON reflection response raises PlanningError."""
    reflector = Reflector(MockLLMClient("not json at all"))

    with pytest.raises(PlanningError, match="Failed to parse"):
        await reflector.reflect("task", "results")


@pytest.mark.asyncio
async def test_reflect_sets_reasoning_intent():
    llm = MockLLMClient(_make_reflection_json(score=9))
    reflector = Reflector(llm)

    await reflector.reflect("task", "results")

    assert llm.last_intent.purpose is Purpose.REFLECT
    assert llm.last_intent.requires_reasoning is True


# ---------------------------------------------------------------------------
# Tests: frozen dataclass
# ---------------------------------------------------------------------------

def test_reflection_result_is_frozen():
    """ReflectionResult is immutable."""
    rr = ReflectionResult(
        completeness_score=8,
        missing_aspects=(),
        additional_sub_goals=(),
    )
    with pytest.raises(AttributeError):
        rr.completeness_score = 5  # type: ignore[misc]
