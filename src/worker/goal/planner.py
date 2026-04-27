"""
Goal planner - milestone advancement, goal-to-duty conversion,
multi-goal prioritization, and resource conflict detection.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from src.services.llm.intent import LLMCallIntent, Purpose

from .models import (
    PRIORITY_MAP,
    Goal,
    GoalPrioritySortKey,
    GoalTask,
    Milestone,
    _parse_date,
)


# ---------------------------------------------------------------------------
# DutyFromGoal intermediate representation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DutyFromGoal:
    """Goal -> Duty conversion intermediate representation."""
    duty_id: str
    title: str
    action: str
    quality_criteria: tuple[str, ...]
    skill_hint: str | None
    preferred_skill_ids: tuple[str, ...] = ()
    schedule_cron: str | None = None


# ---------------------------------------------------------------------------
# Milestone auto-advance
# ---------------------------------------------------------------------------

def auto_advance_milestone(goal: Goal, milestone_id: str) -> Goal:
    """
    Auto-advance milestone and goal status based on task completion.

    If all tasks in the milestone are completed:
      - milestone.status -> "completed"
      - milestone.completed_at -> current datetime
    If all milestones are completed:
      - goal.status -> "completed"

    Returns a new Goal (immutable).
    """
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    updated_milestones: list[Milestone] = []
    found = False

    for ms in goal.milestones:
        if ms.id == milestone_id:
            found = True
            ms = _try_complete_milestone(ms, now_str)
        updated_milestones.append(ms)

    if not found:
        raise ValueError(
            f"Milestone '{milestone_id}' not found in goal '{goal.goal_id}'"
        )

    new_milestones = tuple(updated_milestones)
    new_goal = replace(goal, milestones=new_milestones)

    # Check if all milestones are completed
    if new_milestones and all(m.status == "completed" for m in new_milestones):
        new_goal = replace(new_goal, status="completed")

    return new_goal


def approve_goal(goal: Goal) -> Goal:
    """Transition a goal from pending_approval to active."""
    if goal.status != "pending_approval":
        raise ValueError(
            f"Cannot approve goal in status '{goal.status}', "
            f"expected 'pending_approval'"
        )
    return replace(goal, status="active")


def write_goal_md_approval(
    goal: Goal,
    goals_dir: Path,
    filename: str | None = None,
) -> Path:
    """Persist an approved goal using the standard GOAL.md serializer."""
    from src.worker.integrations.goal_generator import write_goal_md

    return write_goal_md(goal=goal, goals_dir=goals_dir, filename=filename)


def _try_complete_milestone(ms: Milestone, now_str: str) -> Milestone:
    """Complete a milestone if all its tasks are completed."""
    if not ms.tasks:
        return ms
    if all(t.status == "completed" for t in ms.tasks):
        return replace(ms, status="completed", completed_at=now_str)
    return ms


# ---------------------------------------------------------------------------
# Goal -> Duty conversion
# ---------------------------------------------------------------------------

async def goal_to_duty(
    goal: Goal,
    llm_client: object,
) -> DutyFromGoal | None:
    """
    Convert a completed goal to a duty (if on_complete="create_duty").

    Uses LLM to extract maintenance duty content from the goal.
    Returns None if on_complete is not "create_duty" or goal is not completed.
    """
    if goal.on_complete != "create_duty":
        return None
    if goal.status != "completed":
        return None

    # Build a summary of what the goal accomplished
    milestone_titles = ", ".join(m.title for m in goal.milestones)
    task_titles = ", ".join(
        t.title for m in goal.milestones for t in m.tasks
    )

    # LLM call to generate duty content
    prompt = (
        f"Based on the completed goal '{goal.title}' with milestones "
        f"[{milestone_titles}] and tasks [{task_titles}], "
        f"generate a concise maintenance duty action description."
    )

    try:
        response = await llm_client.invoke(
            messages=[
                {"role": "system", "content": "You generate concise duty descriptions."},
                {"role": "user", "content": prompt},
            ],
            intent=LLMCallIntent(purpose=Purpose.PLAN),
        )
        action_text = response.content if response.content else f"Maintain outcomes of {goal.title}"
    except Exception:
        action_text = f"Maintain outcomes of {goal.title}"

    from src.worker.lifecycle.duty_builder import stable_duty_id

    return DutyFromGoal(
        duty_id=stable_duty_id(goal.goal_id, prefix="duty-goal"),
        title=f"Maintain: {goal.title}",
        action=action_text,
        quality_criteria=(f"Outcomes of {goal.title} are sustained",),
        skill_hint=None,
        preferred_skill_ids=goal.preferred_skill_ids,
    )


# ---------------------------------------------------------------------------
# Multi-goal prioritization
# ---------------------------------------------------------------------------

def prioritize_goals(
    goals: tuple[Goal, ...],
    current_date: str,
) -> tuple[Goal, ...]:
    """
    Pure function: sort goals by priority -> deadline urgency -> goal_id.

    Priority: high=0, medium=1, low=2 (ascending).
    Deadline urgency: days until deadline (lower = more urgent).
    Goals without deadlines get a high urgency value (999999).
    """
    def sort_key(g: Goal) -> GoalPrioritySortKey:
        pv = PRIORITY_MAP.get(g.priority, 2)
        urgency = _deadline_urgency(g.deadline, current_date)
        return GoalPrioritySortKey(
            priority_value=pv,
            deadline_urgency=urgency,
            goal_id=g.goal_id,
        )

    sorted_goals = sorted(
        goals,
        key=lambda g: (
            sort_key(g).priority_value,
            sort_key(g).deadline_urgency,
            sort_key(g).goal_id,
        ),
    )
    return tuple(sorted_goals)


def _deadline_urgency(deadline: str | None, current_date: str) -> float:
    """Compute days until deadline. Lower = more urgent."""
    if deadline is None:
        return 999999.0
    try:
        dl = _parse_date(deadline)
        cur = _parse_date(current_date)
        return float((dl - cur).days)
    except (ValueError, TypeError):
        return 999999.0


# ---------------------------------------------------------------------------
# Resource conflict detection
# ---------------------------------------------------------------------------

def detect_resource_conflicts(
    goals: tuple[Goal, ...],
) -> tuple[tuple[str, str], ...]:
    """
    Pure function: detect skill competition between goals.

    Two goals are treated as conflicting when they declare at least one
    overlapping preferred skill. Returned pairs are normalized and sorted.
    """
    conflicts: set[tuple[str, str]] = set()
    for index, left in enumerate(goals):
        left_skills = {
            skill_id.strip()
            for skill_id in left.preferred_skill_ids
            if str(skill_id).strip()
        }
        if not left_skills:
            continue
        for right in goals[index + 1:]:
            right_skills = {
                skill_id.strip()
                for skill_id in right.preferred_skill_ids
                if str(skill_id).strip()
            }
            if not right_skills:
                continue
            if left_skills & right_skills:
                conflicts.add(tuple(sorted((left.goal_id, right.goal_id))))
    return tuple(sorted(conflicts))
