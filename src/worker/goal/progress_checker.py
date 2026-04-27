"""
Goal progress checker - pure functions for deviation scoring and action recommendation.

Core algorithm:
  deviation = w1*overdue_ratio + w2*stall_ratio + w3*(1-progress_ratio)
  deviation >= 0.7 -> escalate
  deviation >= 0.4 -> adjust_plan
  otherwise -> proceed
"""
from __future__ import annotations

from datetime import datetime

from .models import Goal, GoalCheckResult, GoalTask, Milestone, _collect_all_tasks

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STALL_THRESHOLD_DAYS = 7
DEVIATION_OVERDUE_WEIGHT = 0.4
DEVIATION_STALL_WEIGHT = 0.3
DEVIATION_PROGRESS_WEIGHT = 0.3
ESCALATION_THRESHOLD = 0.7
ADJUST_PLAN_THRESHOLD = 0.4


# ---------------------------------------------------------------------------
# Pure scoring functions
# ---------------------------------------------------------------------------

def compute_deviation_score(goal: Goal, current_date: str) -> float:
    """
    Pure function: three-dimensional weighted deviation score.

    deviation = w1*overdue_ratio + w2*stall_ratio + w3*(1-progress_ratio)
    Result clamped to [0.0, 1.0].
    """
    overdue_ratio = _compute_overdue_ratio(goal, current_date)
    stall_ratio = _compute_stall_ratio(goal, current_date)
    progress_ratio = goal.overall_progress

    deviation = (
        DEVIATION_OVERDUE_WEIGHT * overdue_ratio
        + DEVIATION_STALL_WEIGHT * stall_ratio
        + DEVIATION_PROGRESS_WEIGHT * (1.0 - progress_ratio)
    )
    return max(0.0, min(1.0, round(deviation, 4)))


def check_goal_progress(goal: Goal, current_date: str) -> GoalCheckResult:
    """
    Pure function: four checks -> overdue milestones, stalled tasks,
    newly actionable tasks, deviation score -> recommended action.
    """
    overdue_ms = goal.overdue_milestones_at(current_date)
    overdue_ids = tuple(m.id for m in overdue_ms)

    stalled = _find_stalled_tasks(goal, current_date)
    stalled_ids = tuple(t.id for t in stalled)

    actionable = goal.next_actionable_tasks
    actionable_ids = tuple(t.id for t in actionable)

    deviation = compute_deviation_score(goal, current_date)
    action = _recommend_action(deviation)

    return GoalCheckResult(
        goal_id=goal.goal_id,
        overdue_milestones=overdue_ids,
        stalled_tasks=stalled_ids,
        newly_actionable_tasks=actionable_ids,
        deviation_score=deviation,
        recommended_action=action,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _recommend_action(deviation: float) -> str:
    """Map deviation score to recommended action."""
    if deviation >= ESCALATION_THRESHOLD:
        return "escalate"
    if deviation >= ADJUST_PLAN_THRESHOLD:
        return "adjust_plan"
    return "proceed"


def _compute_overdue_ratio(goal: Goal, current_date: str) -> float:
    """Ratio of overdue milestones to total milestones."""
    if not goal.milestones:
        return 0.0
    overdue_count = len(goal.overdue_milestones_at(current_date))
    return overdue_count / len(goal.milestones)


def _compute_stall_ratio(goal: Goal, current_date: str) -> float:
    """Ratio of stalled tasks to total in-progress tasks."""
    all_tasks = _collect_all_tasks(goal.milestones)
    in_progress = [t for t in all_tasks if t.status == "in_progress"]
    if not in_progress:
        return 0.0
    stalled = _find_stalled_tasks(goal, current_date)
    return len(stalled) / len(in_progress)


def _find_stalled_tasks(goal: Goal, current_date: str) -> tuple[GoalTask, ...]:
    """
    Find in_progress tasks that have been stalled beyond STALL_THRESHOLD_DAYS.

    Since GoalTask doesn't carry a start date, we use a heuristic:
    tasks with status "in_progress" and notes containing a date are checked.
    For simplicity, all in_progress tasks without recent activity markers
    are considered stalled if the goal has overdue milestones.

    A more robust implementation would track task start times separately.
    For now: if a milestone is overdue and contains in_progress tasks,
    those tasks are considered stalled.
    """
    stalled: list[GoalTask] = []
    for m in goal.milestones:
        if m.is_overdue_at(current_date):
            for t in m.tasks:
                if t.status == "in_progress":
                    stalled.append(t)
    return tuple(stalled)
