"""
Reflector - evaluates task completion and proposes additional sub-goals.

After sub-goals are executed, the reflector assesses completeness (0-10)
and recommends follow-up sub-goals when the score is below threshold.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from src.services.llm.intent import LLMCallIntent, Purpose

from .models import PlanningError, ReflectionResult, SubGoal
from .prompts import REFLECTION_PROMPT

logger = logging.getLogger(__name__)

# Completeness threshold: scores below this trigger additional iteration
COMPLETENESS_THRESHOLD = 8


class LLMClient(Protocol):
    """Minimal LLM protocol for reflection."""

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        intent: LLMCallIntent | None = None,
    ) -> Any: ...


class Reflector:
    """
    Evaluates execution completeness and proposes additional sub-goals.

    Uses LLM to score completeness (0-10). When score < threshold,
    returns additional SubGoals for further iteration.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        threshold: int = COMPLETENESS_THRESHOLD,
    ) -> None:
        self._llm = llm_client
        self._threshold = threshold

    @property
    def threshold(self) -> int:
        return self._threshold

    async def reflect(
        self,
        original_task: str,
        sub_goal_results: str,
    ) -> ReflectionResult:
        """
        Evaluate completeness of executed sub-goals.

        Args:
            original_task: The original task description.
            sub_goal_results: Formatted results from executed sub-goals.

        Returns:
            ReflectionResult with score, missing aspects, and additional goals.
        """
        prompt = REFLECTION_PROMPT.format(
            original_task=original_task,
            sub_goal_results=sub_goal_results,
        )

        response = await self._llm.invoke(
            messages=[{"role": "user", "content": prompt}],
            intent=LLMCallIntent(
                purpose=Purpose.REFLECT,
                requires_reasoning=True,
                quality_critical=True,
            ),
        )

        raw = _extract_content(response)
        return _parse_reflection_response(raw)

    def needs_iteration(self, result: ReflectionResult) -> bool:
        """Check if the reflection result indicates more work is needed."""
        return result.completeness_score < self._threshold


def _extract_content(response: Any) -> str:
    """Extract text content from an LLM response object."""
    if hasattr(response, "content"):
        return response.content
    if isinstance(response, str):
        return response
    return str(response)


def _parse_reflection_response(raw: str) -> ReflectionResult:
    """Parse LLM reflection response into ReflectionResult."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        json_lines = [
            ln for ln in lines
            if not ln.strip().startswith("```")
        ]
        text = "\n".join(json_lines)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanningError(
            f"Failed to parse reflection JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise PlanningError("Reflection response is not a JSON object")

    score = int(data.get("completeness_score", 0))
    score = max(0, min(10, score))

    missing = data.get("missing_aspects", [])
    missing_tuple = tuple(str(m) for m in missing) if isinstance(missing, list) else ()

    raw_goals = data.get("additional_sub_goals", [])
    additional = _build_additional_goals(raw_goals)

    return ReflectionResult(
        completeness_score=score,
        missing_aspects=missing_tuple,
        additional_sub_goals=additional,
    )


def _build_additional_goals(raw_goals: Any) -> tuple[SubGoal, ...]:
    """Convert raw additional goals from LLM into SubGoal tuple."""
    if not isinstance(raw_goals, list):
        return ()

    goals: list[SubGoal] = []
    for i, item in enumerate(raw_goals):
        if isinstance(item, dict):
            goals.append(SubGoal(
                id=item.get("id", f"sg-extra-{i}"),
                description=item.get("description", str(item)),
                skill_hint=item.get("skill_hint"),
                preferred_skill_ids=_parse_preferred_skill_ids(item),
                depends_on=tuple(item.get("depends_on", [])),
            ))
        elif isinstance(item, str):
            goals.append(SubGoal(
                id=f"sg-extra-{i}",
                description=item,
            ))

    return tuple(goals)


def _parse_preferred_skill_ids(item: dict[str, Any]) -> tuple[str, ...]:
    """Parse soft-preferred skills from reflection output."""
    raw = item.get("preferred_skill_ids")
    if raw is None:
        raw = item.get("skills", ())
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    return tuple(
        str(skill_id).strip()
        for skill_id in raw
        if str(skill_id).strip()
    )
