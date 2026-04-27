"""
Goal decomposer - breaks a task into SubGoals via LLM invocation.

Pure orchestration: calls LLM with decomposition prompt, parses response
into SubGoal list, and validates the dependency DAG via topological sort.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from src.services.llm.intent import LLMCallIntent, Purpose

from .models import PlanningError, PlanningResult, SubGoal
from .prompts import DECOMPOSITION_PROMPT
from .subagent.aggregator import topological_sort_to_layers

logger = logging.getLogger(__name__)


class LLMClient(Protocol):
    """Minimal LLM protocol for planning (compatible with engine LLMClient)."""

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        intent: LLMCallIntent | None = None,
    ) -> Any: ...


class Decomposer:
    """
    Decomposes a task into SubGoals using LLM reasoning.

    Produces a PlanningResult with topologically ordered sub-goals.
    Raises PlanningError on cyclic dependencies or invalid LLM output.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        self._llm = llm_client

    async def decompose(
        self,
        task: str,
        worker_name: str = "",
        worker_role: str = "",
        available_skills: str = "",
        episodic_context: str = "",
        rules_context: str = "",
    ) -> PlanningResult:
        """
        Decompose a task into sub-goals via LLM.

        Returns PlanningResult with sub_goals and execution_order.
        Raises PlanningError if the LLM output is unparseable or has cycles.
        """
        prompt = DECOMPOSITION_PROMPT.format(
            worker_name=worker_name or "Worker",
            worker_role=worker_role or "assistant",
            task=task,
            available_skills=available_skills or "none",
            episodic_context=episodic_context or "none",
            rules_context=rules_context or "none",
        )

        response = await self._llm.invoke(
            messages=[{"role": "user", "content": prompt}],
            intent=LLMCallIntent(
                purpose=Purpose.PLAN,
                quality_critical=True,
            ),
        )

        raw = _extract_content(response)
        parsed = _parse_decomposition_response(raw)
        sub_goals = _build_sub_goals(parsed)

        if not sub_goals:
            raise PlanningError("Decomposition produced no sub-goals")

        # Validate DAG and compute execution order
        layers = topological_sort_to_layers(sub_goals)
        execution_order = tuple(
            sg_id for layer in layers for sg_id in layer
        )

        reasoning = parsed.get("reasoning", "")

        return PlanningResult(
            sub_goals=sub_goals,
            execution_order=execution_order,
            reasoning=reasoning,
        )


def _extract_content(response: Any) -> str:
    """Extract text content from an LLM response object."""
    if hasattr(response, "content"):
        return response.content
    if isinstance(response, str):
        return response
    return str(response)


def _parse_decomposition_response(raw: str) -> dict[str, Any]:
    """Parse JSON from LLM response, stripping markdown fences if present."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove first and last fence lines
        json_lines = [
            ln for ln in lines
            if not ln.strip().startswith("```")
        ]
        text = "\n".join(json_lines)

    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanningError(f"Failed to parse decomposition JSON: {exc}") from exc

    if not isinstance(result, dict):
        raise PlanningError("Decomposition response is not a JSON object")

    return result


def _build_sub_goals(parsed: dict[str, Any]) -> tuple[SubGoal, ...]:
    """Convert parsed JSON into SubGoal tuple."""
    raw_goals = parsed.get("sub_goals", [])
    if not isinstance(raw_goals, list):
        raise PlanningError("'sub_goals' field must be a list")

    goals: list[SubGoal] = []
    for i, item in enumerate(raw_goals):
        if not isinstance(item, dict):
            raise PlanningError(f"Sub-goal at index {i} is not an object")

        goal_id = item.get("id", f"sg-{i}")
        description = item.get("description", "")
        skill_hint = item.get("skill_hint")
        preferred_skill_ids = _parse_preferred_skill_ids(item)
        depends_raw = item.get("depends_on", [])
        depends_on = tuple(depends_raw) if isinstance(depends_raw, list) else ()

        goals.append(SubGoal(
            id=str(goal_id),
            description=str(description),
            skill_hint=skill_hint,
            preferred_skill_ids=preferred_skill_ids,
            depends_on=depends_on,
        ))

    return tuple(goals)


def _parse_preferred_skill_ids(item: dict[str, Any]) -> tuple[str, ...]:
    """Parse soft-preferred skills from decomposition output."""
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
