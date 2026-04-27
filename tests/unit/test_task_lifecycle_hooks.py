# edition: baseline
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.engine.protocols import LLMResponse, UsageInfo
from src.engine.state import WorkerContext
from src.memory.episodic.store import IndexFileLock, load_episode, load_index
from src.memory.preferences.extractor import load_active_decisions
from src.runtime.task_hooks import build_post_run_handler
from src.worker.goal.models import Goal, GoalTask, Milestone
from src.worker.integrations.goal_generator import write_goal_md
from src.worker.lifecycle.goal_projector import GoalLockRegistry
from src.worker.lifecycle.models import SuggestionRecord, add_days_iso, now_iso
from src.worker.lifecycle.suggestion_store import SuggestionStore
from src.worker.task import TaskProvenance, create_task_manifest
from src.worker.task_runner import PostRunExtraction
from src.worker.trust_gate import WorkerTrustGate


class _GoalDutyLLM:
    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
        return LLMResponse(
            content="每周检查目标成果并更新维护状态",
            usage=UsageInfo(total_tokens=10),
        )


def _write_persona(worker_dir: Path) -> None:
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "PERSONA.md").write_text(
        "---\nidentity:\n  worker_id: worker-1\n  name: Worker 1\nprinciples:\n  - Be accurate\n---\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_post_run_handler_links_episode_and_creates_goal_suggestion(tmp_path):
    workspace_root = tmp_path
    worker_dir = workspace_root / "tenants" / "tenant-1" / "workers" / "worker-1"
    _write_persona(worker_dir)
    goal = Goal(
        goal_id="goal-1",
        title="Finish analysis",
        status="active",
        priority="high",
        on_complete="create_duty",
        milestones=(
            Milestone(
                id="ms-1",
                title="Complete final task",
                status="in_progress",
                tasks=(GoalTask(id="gt-1", title="final step", status="pending"),),
            ),
        ),
    )
    write_goal_md(goal, worker_dir / "goals", filename="goal-1.md")
    suggestion_store = SuggestionStore(workspace_root)
    handler = build_post_run_handler(
        workspace_root=workspace_root,
        llm_client=_GoalDutyLLM(),
        episode_lock=IndexFileLock(),
        suggestion_store=suggestion_store,
        goal_lock_registry=GoalLockRegistry(),
    )
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="完成 goal 最后一步",
        provenance=TaskProvenance(
            source_type="goal_task",
            source_id="goal-1",
            goal_id="goal-1",
            goal_task_id="gt-1",
            duty_id="",
        ),
    ).mark_completed("done")

    await handler(
        manifest,
        WorkerContext(worker_id="worker-1", tenant_id="tenant-1"),
        PostRunExtraction(
            episode_summary="完成了最后一步任务",
            key_findings=("收尾完成",),
            tool_names_used=(),
            rule_candidates=(),
            applied_rule_ids=(),
        ),
    )

    index_entries = load_index(worker_dir / "memory")
    assert len(index_entries) == 1
    episode = load_episode(worker_dir / "memory", index_entries[0].id)
    assert episode.related_goals == ("goal-1",)
    assert any(entity.type == "goal_task" and entity.value == "gt-1" for entity in episode.related_entities)

    suggestions = suggestion_store.list_pending("tenant-1", "worker-1")
    assert len(suggestions) == 1
    assert suggestions[0].type == "goal_to_duty"
    assert suggestions[0].source_entity_id == "goal-1"


@pytest.mark.asyncio
async def test_post_run_handler_skips_episode_when_episodic_write_disabled(tmp_path):
    workspace_root = tmp_path
    worker_dir = workspace_root / "tenants" / "tenant-1" / "workers" / "worker-1"
    _write_persona(worker_dir)
    handler = build_post_run_handler(
        workspace_root=workspace_root,
        llm_client=None,
        episode_lock=IndexFileLock(),
        suggestion_store=SuggestionStore(workspace_root),
        goal_lock_registry=GoalLockRegistry(),
    )
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="完成一次任务",
    ).mark_completed("done")

    await handler(
        manifest,
        WorkerContext(
            worker_id="worker-1",
            tenant_id="tenant-1",
            trust_gate=WorkerTrustGate(episodic_write_enabled=False),
        ),
        PostRunExtraction(
            episode_summary="完成了任务",
            key_findings=("收尾完成",),
            tool_names_used=(),
            rule_candidates=(),
            applied_rule_ids=(),
        ),
    )

    assert load_index(worker_dir / "memory") == ()


@pytest.mark.asyncio
async def test_post_run_handler_skips_goal_suggestion_when_already_approved(tmp_path):
    workspace_root = tmp_path
    worker_dir = workspace_root / "tenants" / "tenant-1" / "workers" / "worker-1"
    _write_persona(worker_dir)
    goal = Goal(
        goal_id="goal-1",
        title="Finish analysis",
        status="active",
        priority="high",
        on_complete="create_duty",
        milestones=(
            Milestone(
                id="ms-1",
                title="Complete final task",
                status="in_progress",
                tasks=(GoalTask(id="gt-1", title="final step", status="pending"),),
            ),
        ),
    )
    write_goal_md(goal, worker_dir / "goals", filename="goal-1.md")
    suggestion_store = SuggestionStore(workspace_root)
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
    handler = build_post_run_handler(
        workspace_root=workspace_root,
        llm_client=_GoalDutyLLM(),
        episode_lock=IndexFileLock(),
        suggestion_store=suggestion_store,
        goal_lock_registry=GoalLockRegistry(),
    )
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="完成 goal 最后一步",
        provenance=TaskProvenance(
            source_type="goal_task",
            source_id="goal-1",
            goal_id="goal-1",
            goal_task_id="gt-1",
        ),
    ).mark_completed("done")

    await handler(
        manifest,
        WorkerContext(worker_id="worker-1", tenant_id="tenant-1"),
        PostRunExtraction(
            episode_summary="完成了最后一步任务",
            key_findings=("收尾完成",),
            tool_names_used=(),
            rule_candidates=(),
            applied_rule_ids=(),
        ),
    )

    suggestions = suggestion_store.list_pending("tenant-1", "worker-1")
    assert suggestions == ()


@pytest.mark.asyncio
async def test_post_run_handler_links_suggestion_and_parent_task_entities(tmp_path):
    workspace_root = tmp_path
    worker_dir = workspace_root / "tenants" / "tenant-1" / "workers" / "worker-1"
    _write_persona(worker_dir)
    handler = build_post_run_handler(
        workspace_root=workspace_root,
        llm_client=_GoalDutyLLM(),
        episode_lock=IndexFileLock(),
        suggestion_store=SuggestionStore(workspace_root),
        goal_lock_registry=GoalLockRegistry(),
    )
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="预览 lifecycle suggestion 的草稿",
        provenance=TaskProvenance(
            source_type="suggestion_preview",
            source_id="sugg-123",
            suggestion_id="sugg-123",
            parent_task_id="task-parent-1",
        ),
    ).mark_completed("preview done")

    await handler(
        manifest,
        WorkerContext(worker_id="worker-1", tenant_id="tenant-1"),
        PostRunExtraction(
            episode_summary="完成了 suggestion 预览",
            key_findings=("发现一个执行风险",),
            tool_names_used=(),
            rule_candidates=(),
            applied_rule_ids=(),
        ),
    )

    index_entries = load_index(worker_dir / "memory")
    assert len(index_entries) == 1
    episode = load_episode(worker_dir / "memory", index_entries[0].id)
    assert any(
        entity.type == "suggestion" and entity.value == "sugg-123"
        for entity in episode.related_entities
    )
    assert any(
        entity.type == "parent_task" and entity.value == "task-parent-1"
        for entity in episode.related_entities
    )


@pytest.mark.asyncio
async def test_post_run_handler_suggestion_preview_skips_learning_side_effects(tmp_path):
    workspace_root = tmp_path
    worker_dir = workspace_root / "tenants" / "tenant-1" / "workers" / "worker-1"
    _write_persona(worker_dir)
    goal = Goal(
        goal_id="goal-preview",
        title="Preview goal",
        status="active",
        priority="high",
        on_complete="create_duty",
        milestones=(
            Milestone(
                id="ms-1",
                title="Preview step",
                status="in_progress",
                tasks=(GoalTask(id="gt-1", title="preview", status="pending"),),
            ),
        ),
    )
    write_goal_md(goal, worker_dir / "goals", filename="goal-preview.md")
    suggestion_store = SuggestionStore(workspace_root)
    handler = build_post_run_handler(
        workspace_root=workspace_root,
        llm_client=_GoalDutyLLM(),
        episode_lock=IndexFileLock(),
        suggestion_store=suggestion_store,
        goal_lock_registry=GoalLockRegistry(),
    )
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="预览建议草稿",
        provenance=TaskProvenance(
            source_type="suggestion_preview",
            source_id="sugg-preview-1",
            suggestion_id="sugg-preview-1",
            goal_id="goal-preview",
            goal_task_id="gt-1",
        ),
    ).mark_completed("preview done")

    await handler(
        manifest,
        WorkerContext(worker_id="worker-1", tenant_id="tenant-1"),
        PostRunExtraction(
            episode_summary="完成了 suggestion 预览",
            key_findings=("发现一个执行风险",),
            tool_names_used=(),
            rule_candidates=("Always do preview follow-up",),
            applied_rule_ids=("rule-preview",),
        ),
    )

    index_entries = load_index(worker_dir / "memory")
    assert len(index_entries) == 1
    assert suggestion_store.list_pending("tenant-1", "worker-1") == ()
    assert not (worker_dir / "preferences.jsonl").exists()
    assert not (worker_dir / "decisions.jsonl").exists()
    rules_dir = worker_dir / "rules"
    learned_dir = rules_dir / "learned"
    assert not learned_dir.exists() or not tuple(learned_dir.glob("*.md"))
    reloaded_goal = (worker_dir / "goals" / "goal-preview.md").read_text(encoding="utf-8")
    assert "status: active" in reloaded_goal
    assert "status: pending" in reloaded_goal


@pytest.mark.asyncio
async def test_post_run_handler_does_not_duplicate_identical_decision_or_mirror(tmp_path):
    workspace_root = tmp_path
    worker_dir = workspace_root / "tenants" / "tenant-1" / "workers" / "worker-1"
    _write_persona(worker_dir)
    orchestrator = SimpleNamespace(on_memory_write=AsyncMock())
    handler = build_post_run_handler(
        workspace_root=workspace_root,
        llm_client=None,
        episode_lock=IndexFileLock(),
        memory_orchestrator=orchestrator,
        suggestion_store=SuggestionStore(workspace_root),
        goal_lock_registry=GoalLockRegistry(),
    )
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="最终使用 Redis 作为共享存储。",
    ).mark_completed("done")
    extraction = PostRunExtraction(
        episode_summary="",
        key_findings=(),
        tool_names_used=(),
        rule_candidates=(),
        applied_rule_ids=(),
    )

    await handler(
        manifest,
        WorkerContext(worker_id="worker-1", tenant_id="tenant-1"),
        extraction,
    )
    await handler(
        manifest,
        WorkerContext(worker_id="worker-1", tenant_id="tenant-1"),
        extraction,
    )

    decisions = load_active_decisions(worker_dir / "decisions.jsonl")
    assert len(decisions) == 1
    decision_events = [
        call.args[0]
        for call in orchestrator.on_memory_write.await_args_list
        if getattr(call.args[0], "target", "") == "decision"
    ]
    assert len(decision_events) == 1


@pytest.mark.asyncio
async def test_post_run_handler_keeps_local_decision_when_memory_mirror_fails(tmp_path):
    workspace_root = tmp_path
    worker_dir = workspace_root / "tenants" / "tenant-1" / "workers" / "worker-1"
    _write_persona(worker_dir)
    orchestrator = SimpleNamespace(on_memory_write=AsyncMock(side_effect=RuntimeError("boom")))
    handler = build_post_run_handler(
        workspace_root=workspace_root,
        llm_client=None,
        episode_lock=IndexFileLock(),
        memory_orchestrator=orchestrator,
        suggestion_store=SuggestionStore(workspace_root),
        goal_lock_registry=GoalLockRegistry(),
    )
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="最终使用 Redis 作为共享存储。",
    ).mark_completed("done")

    await handler(
        manifest,
        WorkerContext(worker_id="worker-1", tenant_id="tenant-1"),
        PostRunExtraction(
            episode_summary="",
            key_findings=(),
            tool_names_used=(),
            rule_candidates=(),
            applied_rule_ids=(),
        ),
    )

    decisions = load_active_decisions(worker_dir / "decisions.jsonl")
    assert len(decisions) == 1
    orchestrator.on_memory_write.assert_awaited_once()
