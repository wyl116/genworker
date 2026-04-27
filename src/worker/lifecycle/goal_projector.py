"""Project task outcomes back into GOAL.md state."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from src.common.logger import get_logger
from src.worker.goal.models import Goal, GoalTask, Milestone
from src.worker.goal.planner import auto_advance_milestone
from src.worker.goal.parser import parse_goal
from src.worker.integrations.goal_generator import find_goal_file, goal_to_markdown

logger = get_logger()


class GoalLockRegistry:
    """Process-local lock registry keyed by tenant, worker, and goal."""

    def __init__(self) -> None:
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    def get_lock(self, tenant_id: str, worker_id: str, goal_id: str) -> asyncio.Lock:
        key = (tenant_id, worker_id, goal_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock


@dataclass(frozen=True)
class GoalProjectResult:
    """Result of projecting one task outcome into a goal."""

    goal_id: str = ""
    updated: bool = False
    goal_completed: bool = False
    contention: bool = False
    goal: Goal | None = None


async def project_task_outcome_to_goal(
    *,
    manifest,
    worker_dir: Path,
    goal_lock_registry: GoalLockRegistry,
    llm_client: object | None = None,
    timeout_seconds: float = 5.0,
) -> GoalProjectResult:
    """Apply a task result to its linked goal, if any."""
    del llm_client
    goal_id = getattr(getattr(manifest, "provenance", None), "goal_id", "") or ""
    if not goal_id:
        return GoalProjectResult()

    goal_file = find_goal_file(worker_dir / "goals", goal_id)
    if goal_file is None:
        logger.warning("[GoalProjector] Goal file not found for goal_id=%s", goal_id)
        return GoalProjectResult(goal_id=goal_id)

    lock = goal_lock_registry.get_lock(manifest.tenant_id, manifest.worker_id, goal_id)
    acquired = False
    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout_seconds)
        acquired = True
    except asyncio.TimeoutError:
        logger.warning("goal_md_write_contention goal_id=%s", goal_id)
        return GoalProjectResult(goal_id=goal_id, contention=True)

    try:
        goal = parse_goal(goal_file.read_text(encoding="utf-8"))
        updated_goal, updated = _apply_manifest(goal, manifest)
        if not updated:
            return GoalProjectResult(goal_id=goal_id, goal=goal)
        goal_file.write_text(goal_to_markdown(updated_goal), encoding="utf-8")
        return GoalProjectResult(
            goal_id=goal_id,
            updated=True,
            goal_completed=updated_goal.status == "completed" and goal.status != "completed",
            goal=updated_goal,
        )
    finally:
        if acquired:
            lock.release()


def _apply_manifest(goal: Goal, manifest) -> tuple[Goal, bool]:
    goal_task_id = getattr(manifest.provenance, "goal_task_id", "") or ""
    updated = False
    milestones: list[Milestone] = []
    completed_milestone_ids: set[str] = set()
    for milestone in goal.milestones:
        next_milestone = milestone
        if goal_task_id:
            next_tasks: list[GoalTask] = []
            task_changed = False
            for task in milestone.tasks:
                if task.id != goal_task_id:
                    next_tasks.append(task)
                    continue
                task_changed = True
                updated = True
                if manifest.status.value == "completed":
                    next_tasks.append(replace(task, status="completed"))
                    completed_milestone_ids.add(milestone.id)
                else:
                    next_tasks.append(
                        replace(
                            task,
                            status="blocked",
                            notes=(manifest.error_message or manifest.result_summary or task.notes)[:300],
                        )
                    )
            if task_changed:
                next_milestone = replace(milestone, tasks=tuple(next_tasks))
        next_milestone = _refresh_milestone_status(next_milestone)
        milestones.append(next_milestone)
    next_goal = replace(goal, milestones=tuple(milestones))
    for milestone_id in sorted(completed_milestone_ids):
        next_goal = auto_advance_milestone(next_goal, milestone_id)
    if next_goal.milestones and all(ms.status == "completed" for ms in next_goal.milestones):
        next_goal = replace(next_goal, status="completed")
        updated = True
    return next_goal, updated


def _refresh_milestone_status(milestone: Milestone) -> Milestone:
    if not milestone.tasks:
        return milestone
    if all(task.status == "completed" for task in milestone.tasks):
        return replace(
            milestone,
            status="completed",
            completed_at=milestone.completed_at or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )
    if any(task.status in {"completed", "in_progress", "blocked"} for task in milestone.tasks):
        return replace(milestone, status="in_progress")
    return milestone
