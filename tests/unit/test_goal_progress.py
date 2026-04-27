# edition: baseline
"""
Tests for goal progress checker.
"""
from __future__ import annotations

import pytest

from src.worker.goal.models import Goal, GoalTask, Milestone
from src.worker.goal.progress_checker import (
    ADJUST_PLAN_THRESHOLD,
    ESCALATION_THRESHOLD,
    check_goal_progress,
    compute_deviation_score,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_goal(
    milestones: tuple[Milestone, ...] = (),
    status: str = "active",
    priority: str = "high",
    deadline: str | None = None,
) -> Goal:
    return Goal(
        goal_id="test-goal",
        title="Test Goal",
        status=status,
        priority=priority,
        deadline=deadline,
        milestones=milestones,
    )


def _make_milestone(
    ms_id: str,
    tasks: tuple[GoalTask, ...] = (),
    status: str = "in_progress",
    deadline: str | None = None,
    completed_at: str | None = None,
) -> Milestone:
    return Milestone(
        id=ms_id,
        title=f"Milestone {ms_id}",
        status=status,
        deadline=deadline,
        completed_at=completed_at,
        tasks=tasks,
    )


def _task(tid: str, status: str = "pending", blocked_by: tuple[str, ...] = ()) -> GoalTask:
    return GoalTask(id=tid, title=f"Task {tid}", status=status, blocked_by=blocked_by)


# ---------------------------------------------------------------------------
# Deviation score tests
# ---------------------------------------------------------------------------

class TestDeviationScore:
    def test_all_complete_no_deviation(self):
        """Fully completed goal should have minimal deviation."""
        ms = _make_milestone(
            "ms-1",
            tasks=(
                _task("t-1", "completed"),
                _task("t-2", "completed"),
            ),
            status="completed",
            deadline="2026-12-31",
            completed_at="2026-01-01",
        )
        goal = _make_goal(milestones=(ms,))
        score = compute_deviation_score(goal, "2026-01-15")
        # progress=1.0, no overdue, no stalled -> deviation = 0.3*(1-1) = 0
        assert score == pytest.approx(0.0, abs=0.01)

    def test_no_progress_high_deviation(self):
        """No progress and overdue should have high deviation."""
        ms = _make_milestone(
            "ms-1",
            tasks=(
                _task("t-1", "in_progress"),
                _task("t-2", "pending"),
            ),
            status="in_progress",
            deadline="2026-01-01",  # overdue
        )
        goal = _make_goal(milestones=(ms,))
        score = compute_deviation_score(goal, "2026-04-01")
        # overdue_ratio=1.0, stall=1.0 (in_progress in overdue ms),
        # progress=0.0 -> 0.4*1 + 0.3*1 + 0.3*1 = 1.0
        assert score >= ESCALATION_THRESHOLD

    def test_partial_progress_moderate_deviation(self):
        """Partial progress with some overdue should be moderate."""
        ms1 = _make_milestone(
            "ms-1",
            tasks=(_task("t-1", "completed"), _task("t-2", "completed")),
            status="completed",
            deadline="2026-03-01",
            completed_at="2026-02-28",
        )
        ms2 = _make_milestone(
            "ms-2",
            tasks=(_task("t-3", "pending"), _task("t-4", "pending")),
            status="in_progress",
            deadline="2026-06-01",
        )
        goal = _make_goal(milestones=(ms1, ms2))
        score = compute_deviation_score(goal, "2026-04-01")
        # ms1: completed, ms2: 0% done but not overdue
        # overdue=0, stall=0, progress=0.5 -> 0.3*(1-0.5) = 0.15
        assert 0.0 <= score <= 0.5

    def test_score_clamped_to_range(self):
        """Score should always be in [0.0, 1.0]."""
        goal = _make_goal()
        score = compute_deviation_score(goal, "2026-04-01")
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Progress check tests
# ---------------------------------------------------------------------------

class TestCheckGoalProgress:
    def test_proceed_recommendation(self):
        """On-track goal should recommend proceed."""
        ms = _make_milestone(
            "ms-1",
            tasks=(
                _task("t-1", "completed"),
                _task("t-2", "completed"),
            ),
            status="completed",
            deadline="2026-12-31",
            completed_at="2026-01-01",
        )
        goal = _make_goal(milestones=(ms,))
        result = check_goal_progress(goal, "2026-01-15")
        assert result.recommended_action == "proceed"
        assert result.deviation_score < ADJUST_PLAN_THRESHOLD

    def test_escalate_recommendation(self):
        """Severely off-track goal should recommend escalate."""
        ms = _make_milestone(
            "ms-1",
            tasks=(
                _task("t-1", "in_progress"),
                _task("t-2", "pending"),
            ),
            status="in_progress",
            deadline="2026-01-01",
        )
        goal = _make_goal(milestones=(ms,))
        result = check_goal_progress(goal, "2026-04-01")
        assert result.recommended_action == "escalate"
        assert result.deviation_score >= ESCALATION_THRESHOLD

    def test_adjust_plan_recommendation(self):
        """Moderately off-track goal should recommend adjust_plan."""
        ms1 = _make_milestone(
            "ms-1",
            tasks=(_task("t-1", "completed"),),
            status="completed",
            deadline="2026-02-01",
            completed_at="2026-01-31",
        )
        ms2 = _make_milestone(
            "ms-2",
            tasks=(_task("t-2", "in_progress"),),
            status="in_progress",
            deadline="2026-03-01",  # overdue at check date
        )
        goal = _make_goal(milestones=(ms1, ms2))
        result = check_goal_progress(goal, "2026-04-01")
        # 1 of 2 overdue, 1 stalled, 50% progress
        assert result.deviation_score >= ADJUST_PLAN_THRESHOLD

    def test_overdue_milestones_detected(self):
        ms = _make_milestone(
            "ms-1",
            tasks=(_task("t-1", "pending"),),
            status="in_progress",
            deadline="2026-01-01",
        )
        goal = _make_goal(milestones=(ms,))
        result = check_goal_progress(goal, "2026-04-01")
        assert "ms-1" in result.overdue_milestones

    def test_stalled_tasks_detected(self):
        ms = _make_milestone(
            "ms-1",
            tasks=(_task("t-1", "in_progress"),),
            status="in_progress",
            deadline="2026-01-01",
        )
        goal = _make_goal(milestones=(ms,))
        result = check_goal_progress(goal, "2026-04-01")
        assert "t-1" in result.stalled_tasks

    def test_newly_actionable_tasks(self):
        ms = _make_milestone(
            "ms-1",
            tasks=(
                _task("t-1", "completed"),
                _task("t-2", "pending", blocked_by=("t-1",)),
                _task("t-3", "pending"),
            ),
            status="in_progress",
            deadline="2026-12-31",
        )
        goal = _make_goal(milestones=(ms,))
        result = check_goal_progress(goal, "2026-04-01")
        assert "t-2" in result.newly_actionable_tasks
        assert "t-3" in result.newly_actionable_tasks

    def test_empty_goal_returns_proceed(self):
        goal = _make_goal()
        result = check_goal_progress(goal, "2026-04-01")
        assert result.recommended_action == "proceed"
        assert result.goal_id == "test-goal"
