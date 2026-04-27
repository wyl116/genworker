"""
Strategy selector - matches SubGoals to Skills via LLM reasoning.

For each SubGoal, asks the LLM which Skill is best suited.
Falls back to the sub-goal's skill_hint when LLM is unavailable.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from typing import Any, Protocol

from src.services.llm.intent import LLMCallIntent, Purpose

from .models import PlanningError, SubGoal
from .prompts import STRATEGY_SELECTION_PROMPT

logger = logging.getLogger(__name__)


class LLMClient(Protocol):
    """Minimal LLM protocol for strategy selection."""

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        intent: LLMCallIntent | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class StrategyDecision:
    """Decision for a single SubGoal's execution strategy."""
    sub_goal_id: str
    selected_skill: str | None
    reason: str
    delegate_to: str | None = None


class StrategySelector:
    """
    Selects execution strategy for each SubGoal.

    Uses LLM to match sub-goals to available skills.
    Falls back to skill_hint if LLM fails.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        self._llm = llm_client

    async def select(
        self,
        sub_goal: SubGoal,
        candidate_skills: str = "",
    ) -> StrategyDecision:
        """
        Select the best skill for a sub-goal.

        Returns StrategyDecision with selected_skill or delegate_to.
        """
        prompt = STRATEGY_SELECTION_PROMPT.format(
            sub_goal_description=sub_goal.description,
            candidate_skills=candidate_skills or "none",
            preferred_skill_ids=", ".join(sub_goal.soft_preferred_skill_ids) or "none",
        )

        try:
            response = await self._llm.invoke(
                messages=[{"role": "user", "content": prompt}],
                intent=LLMCallIntent(purpose=Purpose.STRATEGIZE),
            )
            raw = _extract_content(response)
            return _parse_strategy_response(sub_goal.id, raw)
        except (PlanningError, Exception) as exc:
            logger.warning(
                f"Strategy selection LLM failed for {sub_goal.id}: {exc}; "
                f"falling back to skill_hint"
            )
            return StrategyDecision(
                sub_goal_id=sub_goal.id,
                selected_skill=(
                    sub_goal.soft_preferred_skill_ids[0]
                    if sub_goal.soft_preferred_skill_ids else None
                ),
                reason="Fallback to preferred_skill_ids/skill_hint due to LLM error",
            )

    async def select_batch(
        self,
        sub_goals: tuple[SubGoal, ...],
        candidate_skills: str = "",
    ) -> tuple[StrategyDecision, ...]:
        """Select strategies for multiple sub-goals sequentially."""
        decisions: list[StrategyDecision] = []
        for sg in sub_goals:
            decision = await self.select(sg, candidate_skills)
            decisions.append(decision)
        return tuple(decisions)


def _extract_content(response: Any) -> str:
    """Extract text content from an LLM response object."""
    if hasattr(response, "content"):
        return response.content
    if isinstance(response, str):
        return response
    return str(response)


def _parse_strategy_response(sub_goal_id: str, raw: str) -> StrategyDecision:
    """Parse LLM strategy selection response."""
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
    except json.JSONDecodeError:
        raise PlanningError(f"Failed to parse strategy JSON for {sub_goal_id}")

    return StrategyDecision(
        sub_goal_id=sub_goal_id,
        selected_skill=data.get("selected_skill"),
        reason=data.get("reason", ""),
        delegate_to=data.get("delegate_to"),
    )
