# edition: baseline
from dataclasses import replace

import pytest

from src.runtime.scheduler_runtime import (
    load_duty_records,
    load_unique_goals,
    run_duty_skill_detection,
    run_duty_drift_detection,
    run_goal_completion_advisor,
    run_task_pattern_detection,
)
from src.worker.duty.execution_log import write_execution_record
from src.worker.duty.models import DutyExecutionRecord
from src.worker.goal.models import Goal, GoalTask, Milestone
from src.worker.integrations.goal_generator import write_goal_md
from src.worker.lifecycle.duty_builder import build_duty_from_payload, write_duty_md
from src.worker.lifecycle.feedback_store import FeedbackStore
from src.worker.lifecycle.models import FeedbackRecord, SuggestionRecord, add_days_iso, now_iso
from src.worker.lifecycle.suggestion_store import SuggestionStore
from src.worker.task import TaskStore, create_task_manifest


class _GoalLLM:
    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
        from src.engine.protocols import LLMResponse, UsageInfo

        return LLMResponse(
            content="每周检查目标结果并持续维护",
            usage=UsageInfo(total_tokens=10),
        )


def test_run_task_pattern_detection_creates_suggestion(tmp_path):
    task_store = TaskStore(tmp_path)
    suggestion_store = SuggestionStore(tmp_path)
    for index in range(5):
        manifest = create_task_manifest(
            worker_id="worker-1",
            tenant_id="tenant-1",
            task_description="检查上周客户反馈汇总",
        ).mark_completed("done")
        task_store.save(
            replace(
                manifest,
                created_at=add_days_iso(now_iso(), -(index + 1)),
            )
        )

    result = run_task_pattern_detection("tenant-1", "worker-1", tmp_path, suggestion_store)

    assert len(result) == 1
    assert result[0].type == "task_to_duty"


def test_run_duty_drift_detection_creates_suggestion(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    duties_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "duties"
    duty = build_duty_from_payload(
        {
            "duty_id": "duty-1",
            "title": "日报整理",
            "schedule": "0 9 * * *",
            "action": "整理日报内容",
            "quality_criteria": ["完整", "准确"],
        }
    )
    write_duty_md(duty, duties_dir, filename="legacy-title.md")
    duty_log_dir = duties_dir / duty.duty_id
    for index in range(3):
        write_execution_record(
            duty_log_dir,
            DutyExecutionRecord(
                execution_id=f"exec-{index}",
                duty_id="duty-1",
                trigger_id="cron-1",
                depth="standard",
                executed_at=f"2026-04-0{index + 1}T09:00:00+00:00",
                duration_seconds=1.0,
                conclusion="bad result",
            ),
        )
        feedback_store.append(
            "tenant-1",
            "worker-1",
            FeedbackRecord(
                feedback_id=f"fb-{index}",
                target_type="duty",
                target_id="duty-1",
                verdict="rejected",
                reason="结果不对",
                created_by="user:test",
            ),
        )

    result = run_duty_drift_detection(
        "tenant-1",
        "worker-1",
        tmp_path,
        suggestion_store,
        feedback_store,
    )

    assert len(result) == 1
    assert result[0].type == "duty_redefine"
    assert result[0].source_entity_id == "duty-1"


def test_run_duty_skill_detection_creates_suggestion(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    duties_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "duties"
    duty = build_duty_from_payload(
        {
            "duty_id": "duty-1",
            "title": "日报整理",
            "schedule": "0 9 * * *",
            "action": "整理日报内容",
            "quality_criteria": ["完整", "准确"],
        }
    )
    write_duty_md(duty, duties_dir, filename="legacy-title.md")
    duty_log_dir = duties_dir / duty.duty_id
    for index in range(10):
        write_execution_record(
            duty_log_dir,
            DutyExecutionRecord(
                execution_id=f"exec-{index}",
                duty_id="duty-1",
                trigger_id="cron-1",
                depth="standard",
                executed_at=f"2026-04-{index + 1:02d}T09:00:00+00:00",
                duration_seconds=1.0,
                conclusion="ok",
            ),
        )

    result = run_duty_skill_detection(
        "tenant-1",
        "worker-1",
        tmp_path,
        suggestion_store,
    )

    assert len(result) == 1
    assert result[0].type == "duty_to_skill"


@pytest.mark.asyncio
async def test_run_goal_completion_advisor_creates_suggestion(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    worker_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1"
    goal = Goal(
        goal_id="goal-1",
        title="Finish rollout",
        status="completed",
        priority="high",
        on_complete="create_duty",
        milestones=(
            Milestone(
                id="ms-1",
                title="done",
                status="completed",
                tasks=(GoalTask(id="gt-1", title="done", status="completed"),),
            ),
        ),
    )
    write_goal_md(goal, worker_dir / "goals", filename="goal-1.md")

    result = await run_goal_completion_advisor(
        "tenant-1",
        "worker-1",
        tmp_path,
        suggestion_store,
        _GoalLLM(),
    )

    assert len(result) == 1
    assert result[0].type == "goal_to_duty"


@pytest.mark.asyncio
async def test_run_goal_completion_advisor_skips_goal_with_approved_suggestion(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    worker_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1"
    goal = Goal(
        goal_id="goal-1",
        title="Finish rollout",
        status="completed",
        priority="high",
        on_complete="create_duty",
        milestones=(
            Milestone(
                id="ms-1",
                title="done",
                status="completed",
                tasks=(GoalTask(id="gt-1", title="done", status="completed"),),
            ),
        ),
    )
    write_goal_md(goal, worker_dir / "goals", filename="goal-1.md")
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-goal-1",
            type="goal_to_duty",
            source_entity_type="goal",
            source_entity_id="goal-1",
            title="goal",
            reason="done",
            expires_at=add_days_iso(now_iso(), 30),
        ),
    )
    suggestion_store.resolve(
        "tenant-1",
        "worker-1",
        "sugg-goal-1",
        status="approved",
        resolved_by="user:test",
    )

    result = await run_goal_completion_advisor(
        "tenant-1",
        "worker-1",
        tmp_path,
        suggestion_store,
        _GoalLLM(),
    )

    assert result == ()


@pytest.mark.asyncio
async def test_run_goal_completion_advisor_skips_duplicate_goal_definitions(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    worker_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1"
    goal = Goal(
        goal_id="goal-dup",
        title="Finish rollout",
        status="completed",
        priority="high",
        on_complete="create_duty",
        milestones=(
            Milestone(
                id="ms-1",
                title="done",
                status="completed",
                tasks=(GoalTask(id="gt-1", title="done", status="completed"),),
            ),
        ),
    )
    write_goal_md(goal, worker_dir / "goals", filename="goal-a.md")
    write_goal_md(goal, worker_dir / "goals", filename="goal-b.md")

    result = await run_goal_completion_advisor(
        "tenant-1",
        "worker-1",
        tmp_path,
        suggestion_store,
        _GoalLLM(),
    )

    assert len(result) == 1
    assert result[0].source_entity_id == "goal-dup"


def test_load_duty_records_uses_canonical_duty_id_from_definition(tmp_path):
    duties_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "duties"
    duty = build_duty_from_payload(
        {
            "duty_id": "canonical-duty-1",
            "title": "日报整理",
            "schedule": "0 9 * * *",
            "action": "整理日报内容",
            "quality_criteria": ["完整", "准确"],
        }
    )
    write_duty_md(duty, duties_dir, filename="legacy-title.md")
    write_execution_record(
        duties_dir / duty.duty_id,
        DutyExecutionRecord(
            execution_id="exec-1",
            duty_id=duty.duty_id,
            trigger_id="cron-1",
            depth="standard",
            executed_at="2026-04-01T09:00:00+00:00",
            duration_seconds=1.0,
            conclusion="ok",
        ),
    )
    write_execution_record(
        duties_dir / "orphan-dir",
        DutyExecutionRecord(
            execution_id="exec-orphan",
            duty_id="orphan-dir",
            trigger_id="cron-1",
            depth="standard",
            executed_at="2026-04-01T09:00:00+00:00",
            duration_seconds=99.0,
            conclusion="orphan",
        ),
    )

    records = load_duty_records(duties_dir)

    assert len(records) == 1
    assert records[0].duty_id == "canonical-duty-1"


def test_load_unique_goals_skips_duplicate_goal_ids(tmp_path):
    goals_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "goals"
    goal = Goal(
        goal_id="goal-1",
        title="Finish rollout",
        status="active",
        priority="high",
        milestones=(),
    )
    write_goal_md(goal, goals_dir, filename="goal-a.md")
    write_goal_md(goal, goals_dir, filename="goal-b.md")

    records = load_unique_goals(goals_dir)

    assert len(records) == 1
    assert records[0][0].name == "goal-a.md"
    assert records[0][1].goal_id == "goal-1"
