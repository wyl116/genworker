# edition: baseline
from dataclasses import replace

import pytest

from src.worker.goal.models import Goal, GoalTask, Milestone
from src.worker.integrations.goal_generator import write_goal_md
from src.worker.lifecycle.goal_projector import GoalLockRegistry, project_task_outcome_to_goal
from src.worker.task import TaskProvenance, create_task_manifest


@pytest.mark.asyncio
async def test_project_task_outcome_completes_goal(tmp_path):
    worker_dir = tmp_path / "tenants" / "demo" / "workers" / "worker-1"
    goal = Goal(
        goal_id="goal-1",
        title="Finish rollout",
        status="active",
        priority="high",
        on_complete="create_duty",
        milestones=(
            Milestone(
                id="ms-1",
                title="Do work",
                status="in_progress",
                tasks=(
                    GoalTask(id="task-1", title="final step", status="pending"),
                ),
            ),
        ),
    )
    write_goal_md(goal, worker_dir / "goals", filename="goal-1.md")
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="demo",
        task_description="finish final step",
        provenance=TaskProvenance(
            source_type="goal_task",
            source_id="goal-1",
            goal_id="goal-1",
            goal_task_id="task-1",
        ),
    ).mark_completed("done")

    result = await project_task_outcome_to_goal(
        manifest=manifest,
        worker_dir=worker_dir,
        goal_lock_registry=GoalLockRegistry(),
    )

    assert result.updated is True
    assert result.goal_completed is True
    assert result.goal is not None
    assert result.goal.status == "completed"
    assert result.goal.milestones[0].tasks[0].status == "completed"
