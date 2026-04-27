# edition: baseline
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.channels.commands.builtin import build_builtin_command_registry
from src.channels.commands.models import CommandContext
from src.channels.models import ChannelInboundMessage, build_channel_binding
from src.common.tenant import Tenant, TrustLevel
from src.engine.protocols import LLMResponse, UsageInfo
from src.engine.state import WorkerContext
from src.memory.episodic.store import IndexFileLock
from src.runtime.task_hooks import build_post_run_handler
from src.worker.goal.models import Goal, GoalTask, Milestone
from src.worker.goal.parser import parse_goal
from src.worker.integrations.goal_generator import write_goal_md
from src.worker.lifecycle.feedback_store import FeedbackStore
from src.worker.lifecycle.goal_projector import GoalLockRegistry
from src.worker.lifecycle.suggestion_store import SuggestionStore
from src.worker.task import TaskProvenance, create_task_manifest
from src.worker.task_runner import PostRunExtraction


class _LifecycleLLM:
    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
        return LLMResponse(
            content="每周检查成果状态并更新维护摘要",
            usage=UsageInfo(total_tokens=10),
        )


class _StubSessionManager:
    async def find_by_thread(self, thread_id: str):
        return SimpleNamespace(messages=())

    async def reset_thread(self, thread_id: str):
        return None


class _StubTriggerManager:
    def __init__(self) -> None:
        self.registered = []

    async def register_duty(self, duty, tenant_id: str, worker_id: str) -> None:
        self.registered.append((duty, tenant_id, worker_id))

    async def unregister_duty(self, duty_id: str) -> None:
        return None


def _write_persona(worker_dir: Path) -> None:
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "PERSONA.md").write_text(
        "---\nidentity:\n  worker_id: worker-1\n  name: Worker 1\nprinciples:\n  - Be accurate\n---\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_goal_completed_then_approve_suggestion_materializes_duty(tmp_path):
    workspace_root = tmp_path
    worker_dir = workspace_root / "tenants" / "tenant-1" / "workers" / "worker-1"
    _write_persona(worker_dir)
    goal = Goal(
        goal_id="goal-lifecycle-1",
        title="Finish customer research",
        status="active",
        priority="high",
        on_complete="create_duty",
        milestones=(
            Milestone(
                id="ms-1",
                title="Close final task",
                status="in_progress",
                tasks=(GoalTask(id="gt-1", title="close final step", status="pending"),),
            ),
        ),
    )
    goal_path = write_goal_md(goal, worker_dir / "goals", filename="goal-lifecycle.md")
    suggestion_store = SuggestionStore(workspace_root)
    feedback_store = FeedbackStore(workspace_root)
    trigger_manager = _StubTriggerManager()
    handler = build_post_run_handler(
        workspace_root=workspace_root,
        llm_client=_LifecycleLLM(),
        episode_lock=IndexFileLock(),
        suggestion_store=suggestion_store,
        goal_lock_registry=GoalLockRegistry(),
    )

    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="完成 goal 最后一个 task",
        provenance=TaskProvenance(
            source_type="goal_task",
            source_id="goal-lifecycle-1",
            goal_id="goal-lifecycle-1",
            goal_task_id="gt-1",
        ),
    ).mark_completed("done")

    await handler(
        manifest,
        WorkerContext(worker_id="worker-1", tenant_id="tenant-1"),
        PostRunExtraction(
            episode_summary="最终任务已完成",
            key_findings=("goal completed",),
            tool_names_used=(),
            rule_candidates=(),
            applied_rule_ids=(),
        ),
    )

    updated_goal = parse_goal(goal_path.read_text(encoding="utf-8"))
    assert updated_goal.status == "completed"

    pending = suggestion_store.list_pending("tenant-1", "worker-1")
    assert len(pending) == 1
    suggestion = pending[0]
    assert suggestion.type == "goal_to_duty"
    assert suggestion.payload_dict["source_goal_id"] == "goal-lifecycle-1"

    registry = build_builtin_command_registry()
    binding = build_channel_binding(
        {"type": "feishu", "connection_mode": "webhook", "chat_ids": ["oc_123"]},
        tenant_id="tenant-1",
        worker_id="worker-1",
    )
    content = await registry.resolve("approve_suggestion").handler(
        CommandContext(
            message=ChannelInboundMessage(
                message_id="msg-1",
                channel_type="feishu",
                chat_id="oc_123",
                sender_id="user-1",
                content=f"/approve_suggestion {suggestion.suggestion_id}",
            ),
            binding=binding,
            tenant=Tenant(tenant_id="tenant-1", name="Tenant", trust_level=TrustLevel.STANDARD),
            args={"argv": (suggestion.suggestion_id,), "raw_args": suggestion.suggestion_id},
            session_manager=_StubSessionManager(),
            thread_id="im:feishu:oc_123",
            registry=registry,
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
            trigger_managers={"worker-1": trigger_manager},
            workspace_root=workspace_root,
        )
    )

    assert "已创建 Duty" in content.text
    resolved = suggestion_store.get("tenant-1", "worker-1", suggestion.suggestion_id)
    assert resolved is not None
    assert resolved.status == "approved"
    duties_dir = worker_dir / "duties"
    duty_files = list(duties_dir.glob("*.md"))
    assert len(duty_files) == 1
    assert trigger_manager.registered
