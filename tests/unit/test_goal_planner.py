# edition: baseline
"""
Tests for goal planner: milestone advancement, prioritization, resource conflicts.
"""
from __future__ import annotations

import pytest

from src.worker.goal.models import Goal, GoalTask, Milestone
from src.worker.goal.planner import (
    approve_goal,
    auto_advance_milestone,
    detect_resource_conflicts,
    prioritize_goals,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task(tid: str, status: str = "pending") -> GoalTask:
    return GoalTask(id=tid, title=f"Task {tid}", status=status)


def _milestone(
    ms_id: str,
    tasks: tuple[GoalTask, ...] = (),
    status: str = "in_progress",
) -> Milestone:
    return Milestone(id=ms_id, title=f"Milestone {ms_id}", status=status, tasks=tasks)


def _goal(
    goal_id: str,
    milestones: tuple[Milestone, ...] = (),
    priority: str = "medium",
    deadline: str | None = None,
    status: str = "active",
    preferred_skill_ids: tuple[str, ...] = (),
) -> Goal:
    return Goal(
        goal_id=goal_id,
        title=f"Goal {goal_id}",
        status=status,
        priority=priority,
        deadline=deadline,
        milestones=milestones,
        preferred_skill_ids=preferred_skill_ids,
    )


# ---------------------------------------------------------------------------
# Milestone auto-advance tests
# ---------------------------------------------------------------------------

class TestAutoAdvanceMilestone:
    def test_all_tasks_completed_advances_milestone(self):
        ms = _milestone("ms-1", tasks=(
            _task("t-1", "completed"),
            _task("t-2", "completed"),
        ))
        goal = _goal("g-1", milestones=(ms,))
        updated = auto_advance_milestone(goal, "ms-1")
        assert updated.milestones[0].status == "completed"
        assert updated.milestones[0].completed_at is not None

    def test_incomplete_tasks_no_advance(self):
        ms = _milestone("ms-1", tasks=(
            _task("t-1", "completed"),
            _task("t-2", "pending"),
        ))
        goal = _goal("g-1", milestones=(ms,))
        updated = auto_advance_milestone(goal, "ms-1")
        assert updated.milestones[0].status == "in_progress"

    def test_all_milestones_completed_advances_goal(self):
        ms1 = _milestone("ms-1", tasks=(_task("t-1", "completed"),), status="completed")
        ms2 = _milestone("ms-2", tasks=(_task("t-2", "completed"),))
        goal = _goal("g-1", milestones=(ms1, ms2))
        updated = auto_advance_milestone(goal, "ms-2")
        assert updated.milestones[1].status == "completed"
        assert updated.status == "completed"

    def test_not_all_milestones_completed_goal_stays_active(self):
        ms1 = _milestone("ms-1", tasks=(_task("t-1", "completed"),))
        ms2 = _milestone("ms-2", tasks=(_task("t-2", "pending"),))
        goal = _goal("g-1", milestones=(ms1, ms2))
        updated = auto_advance_milestone(goal, "ms-1")
        assert updated.milestones[0].status == "completed"
        assert updated.status == "active"

    def test_nonexistent_milestone_raises(self):
        goal = _goal("g-1", milestones=(_milestone("ms-1"),))
        with pytest.raises(ValueError, match="not found"):
            auto_advance_milestone(goal, "ms-nonexistent")

    def test_empty_tasks_no_advance(self):
        ms = _milestone("ms-1", tasks=())
        goal = _goal("g-1", milestones=(ms,))
        updated = auto_advance_milestone(goal, "ms-1")
        # No tasks means nothing to complete
        assert updated.milestones[0].status == "in_progress"


class TestApproveGoal:
    def test_pending_approval_transitions_to_active(self):
        goal = _goal("g-1", status="pending_approval")
        approved = approve_goal(goal)
        assert approved.status == "active"

    def test_invalid_status_raises(self):
        goal = _goal("g-1", status="active")
        with pytest.raises(ValueError, match="expected 'pending_approval'"):
            approve_goal(goal)


# ---------------------------------------------------------------------------
# Prioritization tests
# ---------------------------------------------------------------------------

class TestPrioritizeGoals:
    def test_priority_ordering(self):
        g_low = _goal("g-low", priority="low")
        g_high = _goal("g-high", priority="high")
        g_med = _goal("g-med", priority="medium")

        result = prioritize_goals((g_low, g_high, g_med), "2026-04-01")
        ids = [g.goal_id for g in result]
        assert ids == ["g-high", "g-med", "g-low"]

    def test_same_priority_sorted_by_deadline(self):
        g1 = _goal("g-far", priority="high", deadline="2026-12-31")
        g2 = _goal("g-near", priority="high", deadline="2026-04-15")

        result = prioritize_goals((g1, g2), "2026-04-01")
        ids = [g.goal_id for g in result]
        assert ids == ["g-near", "g-far"]

    def test_no_deadline_sorted_last(self):
        g1 = _goal("g-deadline", priority="high", deadline="2026-06-01")
        g2 = _goal("g-no-dl", priority="high")

        result = prioritize_goals((g1, g2), "2026-04-01")
        ids = [g.goal_id for g in result]
        assert ids == ["g-deadline", "g-no-dl"]

    def test_same_priority_same_deadline_sorted_by_id(self):
        g1 = _goal("g-b", priority="medium", deadline="2026-06-01")
        g2 = _goal("g-a", priority="medium", deadline="2026-06-01")

        result = prioritize_goals((g1, g2), "2026-04-01")
        ids = [g.goal_id for g in result]
        assert ids == ["g-a", "g-b"]

    def test_empty_goals(self):
        result = prioritize_goals((), "2026-04-01")
        assert result == ()


# ---------------------------------------------------------------------------
# Resource conflict detection tests
# ---------------------------------------------------------------------------

class TestDetectResourceConflicts:
    def test_overlapping_preferred_skills_conflict(self):
        g1 = _goal("g-1", preferred_skill_ids=("analysis", "report"))
        g2 = _goal("g-2", preferred_skill_ids=("ops", "analysis"))
        g3 = _goal("g-3", preferred_skill_ids=("design",))

        result = detect_resource_conflicts((g1, g2, g3))

        assert result == (("g-1", "g-2"),)

    def test_missing_preferred_skills_no_conflict(self):
        g1 = _goal("g-1")
        g2 = _goal("g-2", preferred_skill_ids=("analysis",))
        result = detect_resource_conflicts((g1, g2))
        assert result == ()
