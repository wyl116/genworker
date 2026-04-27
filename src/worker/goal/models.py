"""
Goal data models - frozen dataclasses for the goal tracking subsystem.

Defines:
- GoalTask: individual task within a milestone
- Milestone: a milestone with tasks and deadline
- ExternalSource: external system reference for a goal
- Goal: complete goal definition from GOAL.md
- GoalCheckResult: progress check output
- GoalPrioritySortKey: stable sort key for multi-goal scheduling
- GoalProposal: worker-proposed goal awaiting approval
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from src.worker.scripts.models import PreScript


# ---------------------------------------------------------------------------
# Task & Milestone
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GoalTask:
    """A single task within a milestone."""
    id: str
    title: str
    status: str  # "pending" | "in_progress" | "completed" | "blocked"
    notes: str = ""
    blocked_by: tuple[str, ...] = ()


ALLOWED_TASK_STATUSES = frozenset({
    "pending", "in_progress", "completed", "blocked",
})

ALLOWED_MILESTONE_STATUSES = frozenset({
    "pending", "in_progress", "completed",
})

ALLOWED_GOAL_STATUSES = frozenset({
    "pending_approval", "active", "paused", "completed", "abandoned",
})

ALLOWED_PRIORITIES = frozenset({"high", "medium", "low"})


@dataclass(frozen=True)
class Milestone:
    """A milestone within a goal, containing tasks."""
    id: str
    title: str
    status: str  # "pending" | "in_progress" | "completed"
    deadline: str | None = None
    completed_at: str | None = None
    tasks: tuple[GoalTask, ...] = ()

    @property
    def progress_ratio(self) -> float:
        """Ratio of completed tasks to total tasks."""
        if not self.tasks:
            return 0.0
        completed = sum(1 for t in self.tasks if t.status == "completed")
        return completed / len(self.tasks)

    @property
    def is_overdue(self) -> bool:
        """True if past deadline and not completed."""
        if self.status == "completed":
            return False
        if self.deadline is None:
            return False
        try:
            dl = _parse_date(self.deadline)
            return date.today() > dl
        except (ValueError, TypeError):
            return False

    def is_overdue_at(self, current_date: str) -> bool:
        """Check overdue against a specific date string (YYYY-MM-DD)."""
        if self.status == "completed":
            return False
        if self.deadline is None:
            return False
        try:
            dl = _parse_date(self.deadline)
            cur = _parse_date(current_date)
            return cur > dl
        except (ValueError, TypeError):
            return False


# ---------------------------------------------------------------------------
# ExternalSource
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExternalSource:
    """External system reference for a goal."""
    type: str  # "email" | "feishu_doc" | "wecom_doc" | ...
    source_uri: str
    last_synced_at: str | None = None
    sync_direction: str = "bidirectional"
    stakeholders: tuple[str, ...] = ()
    sync_schedule: str | None = None


# ---------------------------------------------------------------------------
# Goal
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Goal:
    """Complete goal definition parsed from GOAL.md."""
    goal_id: str
    title: str
    status: str  # "pending_approval" | "active" | "paused" | "completed" | "abandoned"
    priority: str  # "high" | "medium" | "low"
    deadline: str | None = None
    created_by: str = ""
    approved_by: str = ""
    milestones: tuple[Milestone, ...] = ()
    preferred_skill_ids: tuple[str, ...] = ()
    default_pre_script: PreScript | None = None
    on_complete: str | None = None  # "create_duty" | None
    external_source: ExternalSource | None = None

    @property
    def overall_progress(self) -> float:
        """Average progress across all milestones."""
        if not self.milestones:
            return 0.0
        return sum(m.progress_ratio for m in self.milestones) / len(self.milestones)

    @property
    def overdue_milestones(self) -> tuple[Milestone, ...]:
        """Milestones past their deadline and not completed."""
        return tuple(m for m in self.milestones if m.is_overdue)

    def overdue_milestones_at(self, current_date: str) -> tuple[Milestone, ...]:
        """Milestones overdue at a specific date."""
        return tuple(
            m for m in self.milestones if m.is_overdue_at(current_date)
        )

    @property
    def next_actionable_tasks(self) -> tuple[GoalTask, ...]:
        """Pending tasks whose blocked_by dependencies are all completed."""
        all_tasks = _collect_all_tasks(self.milestones)
        completed_ids = frozenset(
            t.id for t in all_tasks if t.status == "completed"
        )
        return tuple(
            t for t in all_tasks
            if t.status == "pending"
            and all(dep in completed_ids for dep in t.blocked_by)
        )


# ---------------------------------------------------------------------------
# Check result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GoalCheckResult:
    """Output of a goal progress check."""
    goal_id: str
    overdue_milestones: tuple[str, ...]
    stalled_tasks: tuple[str, ...]
    newly_actionable_tasks: tuple[str, ...]
    deviation_score: float  # 0.0 (on track) ~ 1.0 (severely off)
    recommended_action: str  # "proceed" | "adjust_plan" | "escalate"


# ---------------------------------------------------------------------------
# Sort key
# ---------------------------------------------------------------------------

PRIORITY_MAP: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class GoalPrioritySortKey:
    """Stable sort key for multi-goal scheduling."""
    priority_value: int  # high=0, medium=1, low=2
    deadline_urgency: float  # lower = more urgent
    goal_id: str


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GoalProposal:
    """Worker-proposed goal awaiting approval."""
    proposed_goal: Goal
    justification: str
    proposed_by: str  # "worker:{worker_id}"
    proposed_at: str
    approval_status: str = "pending"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(date_str: str) -> date:
    """Parse a YYYY-MM-DD date string."""
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def _collect_all_tasks(milestones: tuple[Milestone, ...]) -> tuple[GoalTask, ...]:
    """Flatten all tasks from all milestones."""
    tasks: list[GoalTask] = []
    for m in milestones:
        tasks.extend(m.tasks)
    return tuple(tasks)
