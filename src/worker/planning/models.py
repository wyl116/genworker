"""
Planning data models - frozen dataclasses for goal decomposition and reflection.

Defines:
- SubGoal: an individual sub-goal with dependency tracking
- PlanningResult: output of decomposition (sub-goals + execution order)
- ReflectionResult: output of reflection (completeness score + additional goals)
- PlanningError: domain error for planning failures (e.g. cyclic dependencies)
"""
from __future__ import annotations

from dataclasses import dataclass


class PlanningError(Exception):
    """Raised when planning encounters an unrecoverable error."""


@dataclass(frozen=True)
class SubGoal:
    """A single sub-goal produced by task decomposition."""
    id: str
    description: str
    skill_hint: str | None = None
    preferred_skill_ids: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    status: str = "pending"

    @property
    def soft_preferred_skill_ids(self) -> tuple[str, ...]:
        """Return non-binding preferred skills in priority order."""
        if self.preferred_skill_ids:
            return self.preferred_skill_ids
        if self.skill_hint:
            return (self.skill_hint,)
        return ()


@dataclass(frozen=True)
class PlanningResult:
    """Output of the decomposition phase."""
    sub_goals: tuple[SubGoal, ...]
    execution_order: tuple[str, ...]   # sub_goal IDs in topological order
    reasoning: str                      # LLM reasoning trace


@dataclass(frozen=True)
class ReflectionResult:
    """Output of the reflection phase."""
    completeness_score: int             # 0-10
    missing_aspects: tuple[str, ...]
    additional_sub_goals: tuple[SubGoal, ...]
