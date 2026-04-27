# edition: baseline
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autonomy.inbox import SessionInboxStore
from src.channels.commands.builtin import build_builtin_command_registry
from src.channels.commands.models import CommandContext
from src.channels.models import ChannelInboundMessage, build_channel_binding
from src.common.tenant import Tenant, TrustLevel
from src.events.bus import EventBus
from src.events.models import Subscription
from src.worker.goal.models import Goal
from src.worker.integrations.goal_generator import write_goal_md
from src.worker.lifecycle.feedback_store import FeedbackStore
from src.worker.lifecycle.models import SuggestionRecord
from src.worker.lifecycle.suggestion_store import SuggestionStore
from src.worker.lifecycle.task_confirmation import enqueue_task_confirmation
from src.worker.lifecycle.duty_builder import build_duty_from_payload, write_duty_md
from src.worker.loader import load_worker_entry
from src.worker.registry import build_worker_registry
from src.worker.task import TaskStore, create_task_manifest


class _StubSessionManager:
    def __init__(self, session=None) -> None:
        self.session = session or SimpleNamespace(
            messages=(),
            session_id="session-1",
            thread_id="im:feishu:oc_123",
        )

    async def find_by_thread(self, thread_id: str):
        return self.session

    async def reset_thread(self, thread_id: str):
        return None


class _StubTriggerManager:
    def __init__(self) -> None:
        self.registered = []
        self.unregistered = []

    async def register_duty(self, duty, tenant_id: str, worker_id: str) -> None:
        self.registered.append((duty, tenant_id, worker_id))

    async def unregister_duty(self, duty_id: str) -> None:
        self.unregistered.append(duty_id)


class _StubScheduler:
    def __init__(self) -> None:
        self.jobs = []

    async def submit_task(self, job: dict, priority: int) -> bool:
        self.jobs.append((job, priority))
        return True


class _RejectingScheduler:
    def __init__(self) -> None:
        self.jobs = []

    async def submit_task(self, job: dict, priority: int) -> bool:
        self.jobs.append((job, priority))
        return False


class _ExplodingScheduler:
    async def submit_task(self, job: dict, priority: int) -> bool:
        raise RuntimeError("scheduler offline")


class _SlowScheduler:
    def __init__(self) -> None:
        self.jobs = []
        self.started = asyncio.Event()
        self.unblock = asyncio.Event()

    async def submit_task(self, job: dict, priority: int) -> bool:
        self.jobs.append((job, priority))
        self.started.set()
        await self.unblock.wait()
        return True


class _ExplodingConsumeInboxStore(SessionInboxStore):
    async def mark_consumed(
        self,
        inbox_ids: list[str] | tuple[str, ...],
        *,
        tenant_id: str = "",
        worker_id: str = "",
    ) -> None:
        raise RuntimeError("consume failed")


class _ExplodingRequeueInboxStore(SessionInboxStore):
    async def requeue_processing(
        self,
        inbox_ids: list[str] | tuple[str, ...],
        *,
        tenant_id: str = "",
        worker_id: str = "",
    ) -> None:
        raise RuntimeError("requeue failed")


class _ExplodingTriggerManager(_StubTriggerManager):
    async def register_duty(self, duty, tenant_id: str, worker_id: str) -> None:
        raise RuntimeError("trigger sync failed")


class _ExplodingFeedbackStore:
    def append(self, tenant_id: str, worker_id: str, record) -> None:
        raise RuntimeError("feedback disk full")


class _ExplodingResolveSuggestionStore(SuggestionStore):
    def resolve(
        self,
        tenant_id: str,
        worker_id: str,
        suggestion_id: str,
        *,
        status: str,
        resolved_by: str,
        resolution_note: str = "",
        claim_token: str | None = None,
        **_: object,
    ):
        raise RuntimeError("resolve failed")


class _FailOnceResolveSuggestionStore(SuggestionStore):
    def __init__(self, workspace_root) -> None:
        super().__init__(workspace_root)
        self.resolve_calls = 0

    def resolve(
        self,
        tenant_id: str,
        worker_id: str,
        suggestion_id: str,
        *,
        status: str,
        resolved_by: str,
        resolution_note: str = "",
        claim_token: str | None = None,
        **kwargs: object,
    ):
        self.resolve_calls += 1
        if self.resolve_calls == 1:
            raise RuntimeError("resolve failed once")
        return super().resolve(
            tenant_id,
            worker_id,
            suggestion_id,
            status=status,
            resolved_by=resolved_by,
            resolution_note=resolution_note,
            claim_token=str(claim_token or ""),
        )


class _StubLifecycleServices:
    def __init__(self) -> None:
        self.materialized = []
        self.materialized_skills = []
        self.redefined = []

    def materialize_duty_from_payload(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        payload: dict,
        default_title: str = "",
    ):
        self.materialized.append((tenant_id, worker_id, dict(payload), default_title))
        duty = build_duty_from_payload(payload, default_title=default_title)
        return duty, Path(f"/tmp/{duty.duty_id}.md")

    def apply_duty_redefine_payload(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        duty_id: str,
        payload: dict,
    ):
        self.redefined.append((tenant_id, worker_id, duty_id, dict(payload)))
        return build_duty_from_payload(
            {
                "duty_id": duty_id,
                "title": payload.get("title", "updated duty"),
                "action": payload.get("action", "updated action"),
                "quality_criteria": payload.get("quality_criteria", ["完整"]),
            }
        )

    async def materialize_skill_from_payload(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        payload: dict,
        llm_client=None,
        source_record=None,
    ):
        self.materialized_skills.append((tenant_id, worker_id, dict(payload), source_record))
        skill_id = str(payload.get("skill_id", "") or "skill-facade-1")
        skill = SimpleNamespace(skill_id=skill_id)
        return skill, Path(f"/tmp/{skill_id}/SKILL.md")


class _StubWorkerRouter:
    def __init__(self, worker_registry) -> None:
        self._worker_registry = worker_registry

    def replace_worker_registry(self, worker_registry) -> None:
        self._worker_registry = worker_registry


def _ctx(
    tmp_path,
    *,
    argv=(),
    event_bus=None,
    suggestion_store=None,
    feedback_store=None,
    trigger_manager=None,
    inbox_store=None,
    worker_scheduler=None,
    task_store=None,
    lifecycle_services=None,
    session_manager=None,
    worker_router=None,
):
    binding = build_channel_binding(
        {
            "type": "feishu",
            "connection_mode": "webhook",
            "chat_ids": ["oc_123"],
        },
        tenant_id="tenant-1",
        worker_id="worker-1",
    )
    return CommandContext(
        message=ChannelInboundMessage(
            message_id="msg-1",
            channel_type="feishu",
            chat_id="oc_123",
            sender_id="user-1",
            content="",
        ),
        binding=binding,
        tenant=Tenant(tenant_id="tenant-1", name="Tenant", trust_level=TrustLevel.STANDARD),
        args={"argv": tuple(argv), "raw_args": " ".join(argv)},
        session_manager=session_manager or _StubSessionManager(),
        thread_id="im:feishu:oc_123",
        registry=build_builtin_command_registry(),
        event_bus=event_bus,
        suggestion_store=suggestion_store,
        feedback_store=feedback_store,
        inbox_store=inbox_store,
        trigger_managers={"worker-1": trigger_manager} if trigger_manager is not None else {},
        worker_schedulers={"worker-1": worker_scheduler} if worker_scheduler is not None else {},
        task_store=task_store,
        workspace_root=tmp_path,
        lifecycle_services=lifecycle_services,
        worker_router=worker_router,
    )


def _write_persona(worker_dir: Path) -> None:
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "PERSONA.md").write_text(
        (
            "---\n"
            "identity:\n"
            "  worker_id: worker-1\n"
            "  name: Worker 1\n"
            "  role: analyst\n"
            "default_skill: general-query\n"
            "---\n"
            "Worker instructions.\n"
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_approve_suggestion_materializes_duty_and_feedback(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    trigger_manager = _StubTriggerManager()
    registry = build_builtin_command_registry()
    record = SuggestionRecord(
        suggestion_id="sugg-1",
        type="task_to_duty",
        source_entity_type="task_cluster",
        source_entity_id="cluster-1",
        title="建议 duty",
        reason="repeat",
        candidate_payload=(
            '{"title":"每周检查反馈","schedule":"0 9 * * 1",'
            '"action":"检查反馈并生成摘要","quality_criteria":["完整","准确"]}'
        ),
    )
    suggestion_store.save_pending("tenant-1", "worker-1", record)

    content = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-1",),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
            trigger_manager=trigger_manager,
        )
    )

    assert "已创建 Duty" in content.text
    resolved = suggestion_store.get("tenant-1", "worker-1", "sugg-1")
    assert resolved is not None
    assert resolved.status == "approved"
    feedback = feedback_store.list_for_target(
        "tenant-1",
        "worker-1",
        target_type="suggestion",
        target_id="sugg-1",
    )
    assert feedback and feedback[0].verdict == "approved"
    assert trigger_manager.registered


@pytest.mark.asyncio
async def test_approve_goal_command_activates_goal_and_records_actor(tmp_path):
    registry = build_builtin_command_registry()
    goals_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "goals"
    write_goal_md(
        Goal(
            goal_id="goal-pending-1",
            title="待审批目标",
            status="pending_approval",
            priority="high",
        ),
        goals_dir,
    )

    content = await registry.resolve("approve_goal").handler(
        _ctx(tmp_path, argv=("goal-pending-1",))
    )

    assert "已批准并激活" in content.text
    goal_file = next(goals_dir.glob("*.md"))
    updated = goal_file.read_text(encoding="utf-8")
    assert "status: active" in updated
    assert 'approved_by: "user:user-1"' in updated


@pytest.mark.asyncio
async def test_approve_goal_command_publishes_goal_approved_event(tmp_path):
    registry = build_builtin_command_registry()
    goals_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "goals"
    write_goal_md(
        Goal(
            goal_id="goal-pending-event",
            title="待审批目标",
            status="pending_approval",
            priority="high",
        ),
        goals_dir,
    )
    event_bus = EventBus()
    captured = []

    async def _on_goal_approved(event) -> None:
        captured.append(event)

    event_bus.subscribe(Subscription(
        handler_id="test-goal-approved",
        event_type="goal.approved",
        tenant_id="tenant-1",
        handler=_on_goal_approved,
    ))

    await registry.resolve("approve_goal").handler(
        _ctx(
            tmp_path,
            argv=("goal-pending-event",),
            event_bus=event_bus,
        )
    )

    assert len(captured) == 1
    payload = dict(captured[0].payload)
    assert payload["goal_id"] == "goal-pending-event"
    assert payload["worker_id"] == "worker-1"
    assert payload["tenant_id"] == "tenant-1"
    assert payload["status"] == "active"
    assert payload["approved_by"] == "user:user-1"
    assert str(payload["goal_file"]).endswith(".md")


@pytest.mark.asyncio
async def test_reject_goal_command_marks_goal_abandoned(tmp_path):
    registry = build_builtin_command_registry()
    goals_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "goals"
    write_goal_md(
        Goal(
            goal_id="goal-pending-2",
            title="待拒绝目标",
            status="pending_approval",
            priority="medium",
        ),
        goals_dir,
    )

    content = await registry.resolve("reject_goal").handler(
        _ctx(tmp_path, argv=("goal-pending-2", "缺少", "业务价值"))
    )

    assert "已拒绝" in content.text
    goal_file = next(goals_dir.glob("*.md"))
    updated = goal_file.read_text(encoding="utf-8")
    assert "status: abandoned" in updated
    assert 'approved_by: "user:user-1"' in updated


@pytest.mark.asyncio
async def test_approve_suggestion_still_resolves_when_trigger_sync_fails(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    registry = build_builtin_command_registry()
    record = SuggestionRecord(
        suggestion_id="sugg-trigger-fail",
        type="task_to_duty",
        source_entity_type="task_cluster",
        source_entity_id="cluster-2",
        title="建议 duty",
        reason="repeat",
        candidate_payload=(
            '{"title":"每周检查反馈","schedule":"0 9 * * 1",'
            '"action":"检查反馈并生成摘要","quality_criteria":["完整","准确"]}'
        ),
    )
    suggestion_store.save_pending("tenant-1", "worker-1", record)

    content = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-trigger-fail",),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
            trigger_manager=_ExplodingTriggerManager(),
        )
    )

    assert "未能同步注册触发器" in content.text
    resolved = suggestion_store.get("tenant-1", "worker-1", "sugg-trigger-fail")
    assert resolved is not None
    assert resolved.status == "approved"
    duty_files = list((tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "duties").glob("*.md"))
    assert len(duty_files) == 1
    feedback = feedback_store.list_for_target(
        "tenant-1",
        "worker-1",
        target_type="suggestion",
        target_id="sugg-trigger-fail",
    )
    assert feedback and feedback[0].verdict == "approved"


@pytest.mark.asyncio
async def test_approve_suggestion_still_resolves_when_feedback_write_fails(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    registry = build_builtin_command_registry()
    record = SuggestionRecord(
        suggestion_id="sugg-feedback-fail",
        type="task_to_duty",
        source_entity_type="task_cluster",
        source_entity_id="cluster-3",
        title="建议 duty",
        reason="repeat",
        candidate_payload=(
            '{"title":"每周检查反馈","schedule":"0 9 * * 1",'
            '"action":"检查反馈并生成摘要","quality_criteria":["完整","准确"]}'
        ),
    )
    suggestion_store.save_pending("tenant-1", "worker-1", record)

    content = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-feedback-fail",),
            suggestion_store=suggestion_store,
            feedback_store=_ExplodingFeedbackStore(),
            trigger_manager=_StubTriggerManager(),
        )
    )

    assert "已创建 Duty" in content.text
    resolved = suggestion_store.get("tenant-1", "worker-1", "sugg-feedback-fail")
    assert resolved is not None
    assert resolved.status == "approved"
    duty_files = list((tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "duties").glob("*.md"))
    assert len(duty_files) == 1


@pytest.mark.asyncio
async def test_approve_suggestion_warns_when_resolve_fails_after_materialization(tmp_path):
    suggestion_store = _ExplodingResolveSuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    registry = build_builtin_command_registry()
    record = SuggestionRecord(
        suggestion_id="sugg-resolve-fail",
        type="task_to_duty",
        source_entity_type="task_cluster",
        source_entity_id="cluster-4",
        title="建议 duty",
        reason="repeat",
        candidate_payload=(
            '{"title":"每周检查反馈","schedule":"0 9 * * 1",'
            '"action":"检查反馈并生成摘要","quality_criteria":["完整","准确"]}'
        ),
    )
    suggestion_store.save_pending("tenant-1", "worker-1", record)

    content = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-resolve-fail",),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
            trigger_manager=_StubTriggerManager(),
        )
    )

    assert "已创建 Duty" in content.text
    assert "未能标记 suggestion 已批准" in content.text
    duty_files = list((tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "duties").glob("*.md"))
    assert len(duty_files) == 1
    feedback = feedback_store.list_for_target(
        "tenant-1",
        "worker-1",
        target_type="suggestion",
        target_id="sugg-resolve-fail",
    )
    assert feedback == ()


@pytest.mark.asyncio
async def test_approve_suggestion_is_idempotent_for_approved_record(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    registry = build_builtin_command_registry()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-approved-repeat",
            type="goal_to_duty",
            source_entity_type="goal",
            source_entity_id="goal-approved-repeat",
            title="goal",
            reason="done",
        ),
    )
    suggestion_store.resolve(
        "tenant-1",
        "worker-1",
        "sugg-approved-repeat",
        status="approved",
        resolved_by="user:test",
    )

    content = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-approved-repeat",),
            suggestion_store=suggestion_store,
        )
    )

    assert "已批准" in content.text


@pytest.mark.asyncio
async def test_approve_suggestion_reports_rejected_record_state(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    registry = build_builtin_command_registry()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-rejected-repeat",
            type="goal_to_duty",
            source_entity_type="goal",
            source_entity_id="goal-rejected-repeat",
            title="goal",
            reason="done",
        ),
    )
    suggestion_store.resolve(
        "tenant-1",
        "worker-1",
        "sugg-rejected-repeat",
        status="rejected",
        resolved_by="user:test",
    )

    content = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-rejected-repeat",),
            suggestion_store=suggestion_store,
        )
    )

    assert "已拒绝" in content.text


@pytest.mark.asyncio
async def test_approve_suggestion_reports_claimed_record_state(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    registry = build_builtin_command_registry()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-claimed-repeat",
            type="goal_to_duty",
            source_entity_type="goal",
            source_entity_id="goal-claimed-repeat",
            title="goal",
            reason="done",
        ),
    )
    claimed = suggestion_store.claim_pending("tenant-1", "worker-1", "sugg-claimed-repeat")

    assert claimed is not None
    content = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-claimed-repeat",),
            suggestion_store=suggestion_store,
        )
    )

    assert "正在处理中" in content.text


@pytest.mark.asyncio
async def test_approve_suggestion_prefers_lifecycle_services_facade(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    lifecycle_services = _StubLifecycleServices()
    registry = build_builtin_command_registry()
    record = SuggestionRecord(
        suggestion_id="sugg-facade-1",
        type="task_to_duty",
        source_entity_type="task_cluster",
        source_entity_id="cluster-facade-1",
        title="建议 duty",
        reason="repeat",
        candidate_payload=(
            '{"duty_id":"duty-facade-1","title":"每周检查反馈","schedule":"0 9 * * 1",'
            '"action":"检查反馈并生成摘要","quality_criteria":["完整","准确"]}'
        ),
    )
    suggestion_store.save_pending("tenant-1", "worker-1", record)

    content = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-facade-1",),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
            trigger_manager=_StubTriggerManager(),
            lifecycle_services=lifecycle_services,
        )
    )

    assert "已创建 Duty 'duty-facade-1'" in content.text
    assert len(lifecycle_services.materialized) == 1
    tenant_id, worker_id, payload, default_title = lifecycle_services.materialized[0]
    assert tenant_id == "tenant-1"
    assert worker_id == "worker-1"
    assert payload["duty_id"] == "duty-facade-1"
    assert default_title == "建议 duty"


@pytest.mark.asyncio
async def test_approve_suggestions_with_same_title_do_not_overwrite_duty_files(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    registry = build_builtin_command_registry()
    payload_one = (
        '{"duty_id":"duty-a","title":"重复标题","schedule":"0 9 * * 1",'
        '"action":"动作 A","quality_criteria":["完整"]}'
    )
    payload_two = (
        '{"duty_id":"duty-b","title":"重复标题","schedule":"0 10 * * 1",'
        '"action":"动作 B","quality_criteria":["准确"]}'
    )
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-duty-a",
            type="task_to_duty",
            source_entity_type="task_cluster",
            source_entity_id="cluster-a",
            title="重复标题",
            reason="repeat",
            candidate_payload=payload_one,
        ),
    )
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-duty-b",
            type="task_to_duty",
            source_entity_type="task_cluster",
            source_entity_id="cluster-b",
            title="重复标题",
            reason="repeat",
            candidate_payload=payload_two,
        ),
    )

    for suggestion_id in ("sugg-duty-a", "sugg-duty-b"):
        content = await registry.resolve("approve_suggestion").handler(
            _ctx(
                tmp_path,
                argv=(suggestion_id,),
                suggestion_store=suggestion_store,
                feedback_store=feedback_store,
                trigger_manager=_StubTriggerManager(),
            )
        )
        assert "已创建 Duty" in content.text

    duties_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "duties"
    duty_files = sorted(path.name for path in duties_dir.glob("*.md"))
    assert duty_files == ["duty-a.md", "duty-b.md"]


@pytest.mark.asyncio
async def test_approve_task_to_duty_without_explicit_duty_id_uses_stable_default(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    registry = build_builtin_command_registry()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-stable-duty",
            type="task_to_duty",
            source_entity_type="task_cluster",
            source_entity_id="检查 上周 客户反馈 汇总",
            title="重复标题",
            reason="repeat",
            candidate_payload=(
                '{"title":"重复标题","schedule":"0 9 * * 1",'
                '"action":"检查反馈并生成摘要","quality_criteria":["完整","准确"]}'
            ),
        ),
    )

    content = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-stable-duty",),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
            trigger_manager=_StubTriggerManager(),
        )
    )

    assert "已创建 Duty 'duty-" in content.text
    duties_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "duties"
    duty_files = sorted(path.name for path in duties_dir.glob("*.md"))
    assert len(duty_files) == 1
    assert duty_files[0].startswith("duty-")
    assert duty_files[0].endswith(".md")
    assert duty_files[0].isascii()


@pytest.mark.asyncio
async def test_approve_suggestion_reuses_existing_duty_file_for_same_duty_id(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    registry = build_builtin_command_registry()
    duties_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "duties"
    legacy_duty = build_duty_from_payload(
        {
            "duty_id": "duty-legacy-1",
            "title": "Legacy Title",
            "action": "legacy action",
            "quality_criteria": ["完整"],
        }
    )
    write_duty_md(legacy_duty, duties_dir, filename="legacy-title.md")
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-legacy-duty",
            type="task_to_duty",
            source_entity_type="task_cluster",
            source_entity_id="cluster-legacy",
            title="Legacy Title Updated",
            reason="repeat",
            candidate_payload=(
                '{"duty_id":"duty-legacy-1","title":"Legacy Title Updated",'
                '"schedule":"0 9 * * 1","action":"updated action",'
                '"quality_criteria":["完整","准确"]}'
            ),
        ),
    )

    content = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-legacy-duty",),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
            trigger_manager=_StubTriggerManager(),
        )
    )

    assert "已创建 Duty 'duty-legacy-1'" in content.text
    duty_files = sorted(path.name for path in duties_dir.glob("*.md"))
    assert duty_files == ["legacy-title.md"]
    updated_content = (duties_dir / "legacy-title.md").read_text(encoding="utf-8")
    assert "Legacy Title Updated" in updated_content
    assert "updated action" in updated_content


@pytest.mark.asyncio
async def test_reject_suggestion_records_feedback(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    registry = build_builtin_command_registry()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-2",
            type="goal_to_duty",
            source_entity_type="goal",
            source_entity_id="goal-1",
            title="goal",
            reason="done",
        ),
    )

    content = await registry.resolve("reject_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-2", "暂时", "不需要"),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
        )
    )

    assert "已拒绝" in content.text
    resolved = suggestion_store.get("tenant-1", "worker-1", "sugg-2")
    assert resolved is not None
    assert resolved.status == "rejected"
    feedback = feedback_store.list_for_target(
        "tenant-1",
        "worker-1",
        target_type="suggestion",
        target_id="sugg-2",
    )
    assert feedback and feedback[0].verdict == "rejected"


@pytest.mark.asyncio
async def test_reject_suggestion_releases_claim_when_resolve_fails(tmp_path):
    suggestion_store = _ExplodingResolveSuggestionStore(tmp_path)
    registry = build_builtin_command_registry()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-reject-fail",
            type="goal_to_duty",
            source_entity_type="goal",
            source_entity_id="goal-2",
            title="goal",
            reason="done",
        ),
    )

    with pytest.raises(RuntimeError, match="resolve failed"):
        await registry.resolve("reject_suggestion").handler(
            _ctx(
                tmp_path,
                argv=("sugg-reject-fail", "暂时", "不需要"),
                suggestion_store=suggestion_store,
            )
        )

    pending = suggestion_store.get_pending_active("tenant-1", "worker-1", "sugg-reject-fail")
    assert pending is not None
    assert pending.status == "pending"


@pytest.mark.asyncio
async def test_approve_suggestion_retry_after_resolve_failure_reuses_checkpoint(tmp_path):
    suggestion_store = _FailOnceResolveSuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    registry = build_builtin_command_registry()

    class _CheckpointLifecycleServices:
        def __init__(self) -> None:
            self.calls = 0

        async def materialize_skill_from_payload(
            self,
            *,
            tenant_id: str,
            worker_id: str,
            payload: dict,
            llm_client=None,
            source_record=None,
        ):
            self.calls += 1
            skill_id = str(payload.get("skill_id", "") or "skill-checkpoint-1")
            return SimpleNamespace(skill_id=skill_id), Path(f"/tmp/{skill_id}/SKILL.md")

    lifecycle_services = _CheckpointLifecycleServices()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-approve-retry",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id="duty-1",
            title="checkpoint retry",
            reason="stable",
            candidate_payload='{"skill_id":"skill-checkpoint-1","instructions_seed":"checkpoint"}',
        ),
    )

    first = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-approve-retry",),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
            lifecycle_services=lifecycle_services,
        )
    )

    assert "未能标记 suggestion 已批准" in first.text
    pending = suggestion_store.get_pending_active("tenant-1", "worker-1", "sugg-approve-retry")
    assert pending is not None
    assert pending.approval_stage == "materialized"
    assert lifecycle_services.calls == 1

    second = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-approve-retry",),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
            lifecycle_services=lifecycle_services,
        )
    )

    assert "已创建 Skill 'skill-checkpoint-1'" in second.text
    assert lifecycle_services.calls == 1
    resolved = suggestion_store.get("tenant-1", "worker-1", "sugg-approve-retry")
    assert resolved is not None
    assert resolved.status == "approved"


@pytest.mark.asyncio
async def test_reject_suggestion_refuses_checkpointed_pending_record(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    registry = build_builtin_command_registry()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-checkpoint-pending",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id="duty-2",
            title="checkpoint",
            reason="stable",
            approval_stage="materialized",
            approval_summary="已创建 Skill 'skill-duty-2' 并写入 SKILL.md。",
            approval_artifact_ref="skill:skill-duty-2",
            approval_applied_at="2026-04-18T00:00:00+00:00",
        ),
    )

    content = await registry.resolve("reject_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-checkpoint-pending", "不需要"),
            suggestion_store=suggestion_store,
        )
    )

    assert "当前不能拒绝" in content.text
    pending = suggestion_store.get_pending_active("tenant-1", "worker-1", "sugg-checkpoint-pending")
    assert pending is not None
    assert pending.status == "pending"
    assert pending.approval_stage == "materialized"


@pytest.mark.asyncio
async def test_preview_suggestion_creates_gated_confirmation(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    inbox_store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    registry = build_builtin_command_registry()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-preview-1",
            type="goal_to_duty",
            source_entity_type="goal",
            source_entity_id="goal-123",
            title="建议将 Goal 转为 Duty: 周报维护",
            reason="goal completed",
            candidate_payload=(
                '{"duty_id":"duty-goal-123","title":"每周维护周报","schedule":"0 9 * * 1",'
                '"action":"检查周报数据并更新摘要","quality_criteria":["完整","准确"],'
                '"preferred_skill_ids":["skill_reporting"],"source_goal_id":"goal-123"}'
            ),
        ),
    )

    content = await registry.resolve("preview_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-preview-1",),
            suggestion_store=suggestion_store,
            inbox_store=inbox_store,
        )
    )

    assert "suggestion preview confirmation" in content.text
    confirmations = await inbox_store.list_pending(
        tenant_id="tenant-1",
        worker_id="worker-1",
        event_type="task.confirmation_requested",
        limit=20,
    )
    assert len(confirmations) == 1
    payload = confirmations[0].payload
    assert payload["task_kind"] == "suggestion_preview"
    assert payload["goal_id"] == "goal-123"
    manifest = payload["manifest"]
    assert manifest["gate_level"] == "gated"
    assert manifest["provenance"]["source_type"] == "suggestion_preview"
    assert manifest["provenance"]["suggestion_id"] == "sugg-preview-1"
    assert manifest["preferred_skill_ids"] == ["skill_reporting"]


@pytest.mark.asyncio
async def test_preview_suggestion_reuses_existing_confirmation(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    inbox_store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    registry = build_builtin_command_registry()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-preview-2",
            type="duty_redefine",
            source_entity_type="duty",
            source_entity_id="duty-42",
            title="建议重定义 Duty",
            reason="drift detected",
            candidate_payload=(
                '{"duty_id":"duty-42","recommended_action":"pause",'
                '"suggested_changes":{"status":"paused"}}'
            ),
        ),
    )

    for _ in range(2):
        content = await registry.resolve("preview_suggestion").handler(
            _ctx(
                tmp_path,
                argv=("sugg-preview-2",),
                suggestion_store=suggestion_store,
                inbox_store=inbox_store,
            )
        )
        assert "suggestion preview confirmation" in content.text

    confirmations = await inbox_store.list_pending(
        tenant_id="tenant-1",
        worker_id="worker-1",
        event_type="task.confirmation_requested",
        limit=20,
    )
    assert len(confirmations) == 1
    payload = confirmations[0].payload
    assert payload["duty_id"] == "duty-42"
    assert payload["manifest"]["provenance"]["duty_id"] == "duty-42"


@pytest.mark.asyncio
async def test_approve_suggestion_rejects_expired_pending_record(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    registry = build_builtin_command_registry()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-expired",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id="duty-1",
            title="expired",
            reason="stale",
            candidate_payload='{"skill_id":"skill-expired-1","instructions_seed":"stale"}',
            expires_at="2000-01-01T00:00:00+00:00",
        ),
    )

    content = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-expired",),
            suggestion_store=suggestion_store,
        )
    )

    assert "已过期" in content.text
    resolved = suggestion_store.get("tenant-1", "worker-1", "sugg-expired")
    assert resolved is not None
    assert resolved.status == "expired"


@pytest.mark.asyncio
async def test_preview_suggestion_rejects_expired_pending_record(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    inbox_store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    registry = build_builtin_command_registry()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-expired-preview",
            type="goal_to_duty",
            source_entity_type="goal",
            source_entity_id="goal-1",
            title="expired",
            reason="stale",
            candidate_payload='{"duty_id":"duty-1","title":"stale"}',
            expires_at="2000-01-01T00:00:00+00:00",
        ),
    )

    content = await registry.resolve("preview_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-expired-preview",),
            suggestion_store=suggestion_store,
            inbox_store=inbox_store,
        )
    )

    assert "已过期" in content.text
    confirmations = await inbox_store.list_pending(
        tenant_id="tenant-1",
        worker_id="worker-1",
        event_type="task.confirmation_requested",
        limit=20,
    )
    assert confirmations == ()


@pytest.mark.asyncio
async def test_approve_skill_suggestion_keeps_pending_when_payload_is_invalid(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    registry = build_builtin_command_registry()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-invalid-skill",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id="duty-1",
            title="invalid skill payload",
            reason="broken",
            candidate_payload="{not-json",
        ),
    )

    content = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-invalid-skill",),
            suggestion_store=suggestion_store,
        )
    )

    assert "应用 Skill suggestion 失败" in content.text
    pending = suggestion_store.get("tenant-1", "worker-1", "sugg-invalid-skill")
    assert pending is not None
    assert pending.status == "pending"


@pytest.mark.asyncio
async def test_approve_redefine_suggestion_keeps_pending_when_apply_fails(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    registry = build_builtin_command_registry()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-missing-duty",
            type="duty_redefine",
            source_entity_type="duty",
            source_entity_id="missing-duty",
            title="redefine missing duty",
            reason="drift",
            candidate_payload='{"duty_id":"missing-duty","recommended_action":"pause"}',
        ),
    )

    content = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-missing-duty",),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
            trigger_manager=_StubTriggerManager(),
        )
    )

    assert "未找到" in content.text
    pending = suggestion_store.get("tenant-1", "worker-1", "sugg-missing-duty")
    assert pending is not None
    assert pending.status == "pending"
    feedback = feedback_store.list_for_target(
        "tenant-1",
        "worker-1",
        target_type="suggestion",
        target_id="sugg-missing-duty",
    )
    assert feedback == ()


@pytest.mark.asyncio
async def test_approve_redefine_suggestion_prefers_lifecycle_services_facade(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    lifecycle_services = _StubLifecycleServices()
    registry = build_builtin_command_registry()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-facade-redefine",
            type="duty_redefine",
            source_entity_type="duty",
            source_entity_id="duty-9",
            title="redefine duty",
            reason="drift",
            candidate_payload='{"duty_id":"duty-9","recommended_action":"pause"}',
        ),
    )

    content = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-facade-redefine",),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
            trigger_manager=_StubTriggerManager(),
            lifecycle_services=lifecycle_services,
        )
    )

    assert "已更新 Duty 'duty-9'" in content.text
    assert lifecycle_services.redefined == [
        (
            "tenant-1",
            "worker-1",
            "duty-9",
            {"duty_id": "duty-9", "recommended_action": "pause"},
        )
    ]


@pytest.mark.asyncio
async def test_approve_duty_to_skill_suggestion_materializes_skill_and_binds_duty(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    registry = build_builtin_command_registry()
    duties_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "duties"
    duty = build_duty_from_payload(
        {
            "duty_id": "duty-report-1",
            "title": "周报维护",
            "schedule": "0 9 * * 1",
            "action": "维护周报",
            "quality_criteria": ["完整"],
        }
    )
    write_duty_md(duty, duties_dir, filename="weekly-report.md")
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-skill-duty",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id="duty-report-1",
            title="建议将 Duty 演化为 Skill",
            reason="stable",
            candidate_payload=(
                '{"skill_id":"skill-duty-report-1","name":"skill-duty-report-1",'
                '"description":"自动演化技能","keywords":["周报"],'
                '"instructions_seed":"先检查数据，再输出摘要",'
                '"quality_criteria":["完整"],'
                '"source_type":"duty","source_duty_id":"duty-report-1"}'
            ),
        ),
    )

    content = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-skill-duty",),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
        )
    )

    assert "已创建 Skill 'skill-duty-report-1'" in content.text
    skill_file = (
        tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1"
        / "skills" / "skill-duty-report-1" / "SKILL.md"
    )
    assert skill_file.exists()
    duty_content = (duties_dir / "weekly-report.md").read_text(encoding="utf-8")
    assert "skill_id: skill-duty-report-1" in duty_content


@pytest.mark.asyncio
async def test_approve_rule_to_skill_suggestion_marks_rule_crystallized(tmp_path):
    from src.worker.rules.models import Rule, RuleScope, RuleSource, rule_to_markdown
    from src.worker.rules.rule_manager import load_rules

    suggestion_store = SuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    registry = build_builtin_command_registry()
    rules_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "rules" / "learned"
    rules_dir.mkdir(parents=True, exist_ok=True)
    rules_dir.joinpath("rule-1.md").write_text(
        rule_to_markdown(
            Rule(
                rule_id="rule-1",
                type="learned",
                category="strategy",
                status="active",
                rule="Step 1 collect data. Step 2 summarize findings.",
                reason="stable",
                scope=RuleScope(),
                source=RuleSource(
                    type="self_reflection",
                    evidence="summary",
                    created_at="2026-04-17T00:00:00+00:00",
                ),
                confidence=0.95,
                apply_count=30,
            )
        ),
        encoding="utf-8",
    )
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-skill-rule",
            type="rule_to_skill",
            source_entity_type="rule",
            source_entity_id="rule-1",
            title="建议将规则结晶为 Skill",
            reason="stable",
            candidate_payload=(
                '{"skill_id":"crystallized-rule-1","name":"crystallized-rule-1",'
                '"description":"自动结晶规则技能","keywords":["collect"],'
                '"instructions_seed":"Step 1 collect data. Step 2 summarize findings.",'
                '"instructions_reason":"stable","source_type":"rule","source_rule_id":"rule-1"}'
            ),
        ),
    )

    content = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-skill-rule",),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
        )
    )

    assert "已创建 Skill 'crystallized-rule-1'" in content.text
    loaded_rule = next(
        rule
        for rule in load_rules(
            tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "rules"
        )
        if rule.rule_id == "rule-1"
    )
    assert loaded_rule.status == "crystallized"


@pytest.mark.asyncio
async def test_approve_skill_suggestion_prefers_lifecycle_services_facade(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    lifecycle_services = _StubLifecycleServices()
    registry = build_builtin_command_registry()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-facade-skill",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id="duty-1",
            title="建议将 Duty 演化为 Skill",
            reason="stable",
            candidate_payload='{"skill_id":"skill-facade-1","source_type":"duty","source_duty_id":"duty-1"}',
        ),
    )

    content = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-facade-skill",),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
            lifecycle_services=lifecycle_services,
        )
    )

    assert "已创建 Skill 'skill-facade-1'" in content.text
    assert len(lifecycle_services.materialized_skills) == 1
    tenant_id, worker_id, payload, source_record = lifecycle_services.materialized_skills[0]
    assert tenant_id == "tenant-1"
    assert worker_id == "worker-1"
    assert payload == {"skill_id": "skill-facade-1", "source_type": "duty", "source_duty_id": "duty-1"}
    assert source_record is not None
    assert source_record.suggestion_id == "sugg-facade-skill"


@pytest.mark.asyncio
async def test_approve_skill_suggestion_refreshes_worker_router_registry_immediately(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    registry = build_builtin_command_registry()
    worker_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1"
    duties_dir = worker_dir / "duties"
    _write_persona(worker_dir)
    duty = build_duty_from_payload(
        {
            "duty_id": "duty-report-1",
            "title": "周报维护",
            "schedule": "0 9 * * 1",
            "action": "维护周报",
            "quality_criteria": ["完整"],
        }
    )
    write_duty_md(duty, duties_dir, filename="weekly-report.md")
    initial_entry = load_worker_entry(
        workspace_root=tmp_path,
        tenant_id="tenant-1",
        worker_id="worker-1",
    )
    worker_router = _StubWorkerRouter(
        build_worker_registry([initial_entry], default_worker_id="worker-1")
    )

    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-refresh-skill",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id="duty-report-1",
            title="建议将 Duty 演化为 Skill",
            reason="stable",
            candidate_payload=(
                '{"skill_id":"skill-duty-report-1","name":"skill-duty-report-1",'
                '"description":"自动演化技能","keywords":["周报"],'
                '"instructions_seed":"先检查数据，再输出摘要",'
                '"quality_criteria":["完整"],'
                '"source_type":"duty","source_duty_id":"duty-report-1"}'
            ),
        ),
    )

    content = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-refresh-skill",),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
            worker_router=worker_router,
        )
    )

    assert "已创建 Skill 'skill-duty-report-1'" in content.text
    refreshed_entry = worker_router._worker_registry.get("worker-1")
    assert refreshed_entry is not None
    assert refreshed_entry.skill_registry.get("skill-duty-report-1") is not None


@pytest.mark.asyncio
async def test_approve_suggestion_concurrent_requests_only_materialize_once(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    registry = build_builtin_command_registry()
    started = asyncio.Event()
    unblock = asyncio.Event()

    class _SlowLifecycleServices:
        def __init__(self) -> None:
            self.calls = 0

        async def materialize_skill_from_payload(
            self,
            *,
            tenant_id: str,
            worker_id: str,
            payload: dict,
            llm_client=None,
            source_record=None,
        ):
            self.calls += 1
            started.set()
            await unblock.wait()
            return SimpleNamespace(skill_id=str(payload.get("skill_id", "skill-race-1"))), Path("/tmp/skill-race-1/SKILL.md")

    lifecycle_services = _SlowLifecycleServices()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-race-skill",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id="duty-race-1",
            title="race",
            reason="stable",
            candidate_payload='{"skill_id":"skill-race-1","instructions_seed":"race"}',
        ),
    )

    first_task = asyncio.create_task(
        registry.resolve("approve_suggestion").handler(
            _ctx(
                tmp_path,
                argv=("sugg-race-skill",),
                suggestion_store=suggestion_store,
                feedback_store=feedback_store,
                lifecycle_services=lifecycle_services,
            )
        )
    )
    await started.wait()

    second = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-race-skill",),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
            lifecycle_services=lifecycle_services,
        )
    )

    assert "正在处理中" in second.text
    unblock.set()
    first = await first_task

    assert "已创建 Skill 'skill-race-1'" in first.text
    assert lifecycle_services.calls == 1
    resolved = suggestion_store.get("tenant-1", "worker-1", "sugg-race-skill")
    assert resolved is not None
    assert resolved.status == "approved"


@pytest.mark.asyncio
async def test_approve_suggestion_refreshes_claim_while_long_materialization_runs(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    suggestion_store._CLAIM_TIMEOUT_SECONDS = 0.2
    suggestion_store._CLAIM_HEARTBEAT_SECONDS = 0.05
    feedback_store = FeedbackStore(tmp_path)
    registry = build_builtin_command_registry()
    started = asyncio.Event()

    class _SlowLifecycleServices:
        def __init__(self) -> None:
            self.calls = 0

        async def materialize_skill_from_payload(
            self,
            *,
            tenant_id: str,
            worker_id: str,
            payload: dict,
            llm_client=None,
            source_record=None,
        ):
            self.calls += 1
            started.set()
            await asyncio.sleep(0.35)
            return (
                SimpleNamespace(skill_id=str(payload.get("skill_id", "skill-lease-1"))),
                Path("/tmp/skill-lease-1/SKILL.md"),
            )

    lifecycle_services = _SlowLifecycleServices()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-lease-skill",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id="duty-lease-1",
            title="lease",
            reason="stable",
            candidate_payload='{"skill_id":"skill-lease-1","instructions_seed":"lease"}',
        ),
    )

    first_task = asyncio.create_task(
        registry.resolve("approve_suggestion").handler(
            _ctx(
                tmp_path,
                argv=("sugg-lease-skill",),
                suggestion_store=suggestion_store,
                feedback_store=feedback_store,
                lifecycle_services=lifecycle_services,
            )
        )
    )
    await started.wait()
    await asyncio.sleep(0.25)

    second = await registry.resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-lease-skill",),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
            lifecycle_services=lifecycle_services,
        )
    )
    first = await first_task

    assert "正在处理中" in second.text
    assert "已创建 Skill 'skill-lease-1'" in first.text
    assert lifecycle_services.calls == 1
    assert suggestion_store.get_state("tenant-1", "worker-1", "sugg-lease-skill") == "approved"


@pytest.mark.asyncio
async def test_preview_skill_suggestion_includes_skill_details_and_duty_binding(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    inbox_store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    registry = build_builtin_command_registry()
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-preview-skill",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id="duty-42",
            title="建议将 Duty 演化为 Skill",
            reason="stable",
            candidate_payload=(
                '{"skill_id":"skill-duty-42","description":"自动演化技能",'
                '"keywords":["周报","汇总"],"strategy_mode":"autonomous",'
                '"instructions_seed":"维护周报流程","source_type":"duty","source_duty_id":"duty-42"}'
            ),
        ),
    )

    content = await registry.resolve("preview_suggestion").handler(
        _ctx(
            tmp_path,
            argv=("sugg-preview-skill",),
            suggestion_store=suggestion_store,
            inbox_store=inbox_store,
        )
    )

    assert "suggestion preview confirmation" in content.text
    confirmations = await inbox_store.list_pending(
        tenant_id="tenant-1",
        worker_id="worker-1",
        event_type="task.confirmation_requested",
        limit=20,
    )
    assert len(confirmations) == 1
    payload = confirmations[0].payload
    assert payload["duty_id"] == "duty-42"
    assert "Draft Skill ID: skill-duty-42" in payload["task_description"]
    assert payload["manifest"]["provenance"]["duty_id"] == "duty-42"


@pytest.mark.asyncio
async def test_feedback_command_records_task_feedback(tmp_path):
    feedback_store = FeedbackStore(tmp_path)
    registry = build_builtin_command_registry()

    content = await registry.resolve("feedback").handler(
        _ctx(
            tmp_path,
            argv=("task", "task-1", "reject", "结果不对"),
            feedback_store=feedback_store,
        )
    )

    assert "已记录" in content.text
    feedback = feedback_store.list_for_target(
        "tenant-1",
        "worker-1",
        target_type="task",
        target_id="task-1",
    )
    assert feedback and feedback[0].verdict == "rejected"


@pytest.mark.asyncio
async def test_approve_confirmation_submits_task_and_records_feedback(tmp_path):
    feedback_store = FeedbackStore(tmp_path)
    inbox_store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    task_store = TaskStore(tmp_path)
    worker_scheduler = _StubScheduler()
    registry = build_builtin_command_registry()
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="发送邮件给 Alice 确认进度",
        main_session_key="main:worker-1",
    )
    confirmation = await enqueue_task_confirmation(
        inbox_store=inbox_store,
        manifest=manifest,
        task_description=manifest.task_description,
        target_session_key="main:worker-1",
        task_kind="task",
    )

    content = await registry.resolve("approve_confirmation").handler(
        _ctx(
            tmp_path,
            argv=(confirmation.inbox_id,),
            inbox_store=inbox_store,
            feedback_store=feedback_store,
            worker_scheduler=worker_scheduler,
            task_store=task_store,
        )
    )

    assert "已批准任务" in content.text
    assert len(worker_scheduler.jobs) == 1
    job, priority = worker_scheduler.jobs[0]
    assert priority == 15
    assert job["session_id"] == "session-1"
    assert job["thread_id"] == "im:feishu:oc_123"
    assert job["main_session_key"] == "main:worker-1"
    stored = await inbox_store.get_by_id(
        confirmation.inbox_id,
        tenant_id="tenant-1",
        worker_id="worker-1",
    )
    assert stored is not None
    assert stored.status == "CONSUMED"
    feedback = feedback_store.list_for_target(
        "tenant-1",
        "worker-1",
        target_type="task",
        target_id=manifest.task_id,
    )
    assert feedback and feedback[0].verdict == "approved"


@pytest.mark.asyncio
async def test_reject_confirmation_marks_item_consumed(tmp_path):
    feedback_store = FeedbackStore(tmp_path)
    inbox_store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    task_store = TaskStore(tmp_path)
    registry = build_builtin_command_registry()
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="删除无效记录",
        main_session_key="main:worker-1",
    )
    confirmation = await enqueue_task_confirmation(
        inbox_store=inbox_store,
        manifest=manifest,
        task_description=manifest.task_description,
        target_session_key="main:worker-1",
        task_kind="isolated",
    )

    content = await registry.resolve("reject_confirmation").handler(
        _ctx(
            tmp_path,
            argv=(confirmation.inbox_id, "风险过高"),
            inbox_store=inbox_store,
            feedback_store=feedback_store,
            task_store=task_store,
        )
    )

    assert "已拒绝任务确认" in content.text
    stored = await inbox_store.get_by_id(
        confirmation.inbox_id,
        tenant_id="tenant-1",
        worker_id="worker-1",
    )
    assert stored is not None
    assert stored.status == "CONSUMED"
    stored_task = task_store.load("tenant-1", "worker-1", manifest.task_id)
    assert stored_task is not None
    assert stored_task.status.value == "error"
    assert stored_task.error_message == "Rejected via confirmation: 风险过高"
    feedback = feedback_store.list_for_target(
        "tenant-1",
        "worker-1",
        target_type="task",
        target_id=manifest.task_id,
    )
    assert feedback and feedback[0].verdict == "rejected"


@pytest.mark.asyncio
async def test_approve_confirmation_rejection_marks_task_error(tmp_path):
    feedback_store = FeedbackStore(tmp_path)
    inbox_store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    task_store = TaskStore(tmp_path)
    worker_scheduler = _RejectingScheduler()
    registry = build_builtin_command_registry()
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="发送邮件给 Alice 确认进度",
        main_session_key="main:worker-1",
    )
    confirmation = await enqueue_task_confirmation(
        inbox_store=inbox_store,
        manifest=manifest,
        task_description=manifest.task_description,
        target_session_key="main:worker-1",
        task_kind="task",
    )

    content = await registry.resolve("approve_confirmation").handler(
        _ctx(
            tmp_path,
            argv=(confirmation.inbox_id,),
            inbox_store=inbox_store,
            feedback_store=feedback_store,
            worker_scheduler=worker_scheduler,
            task_store=task_store,
        )
    )

    assert "未进入调度队列" in content.text
    stored_task = task_store.load("tenant-1", "worker-1", manifest.task_id)
    assert stored_task is not None
    assert stored_task.status.value == "error"
    assert stored_task.error_message == "Scheduler quota exhausted"
    stored_confirmation = await inbox_store.get_by_id(
        confirmation.inbox_id,
        tenant_id="tenant-1",
        worker_id="worker-1",
    )
    assert stored_confirmation is not None
    assert stored_confirmation.status == "PENDING"


@pytest.mark.asyncio
async def test_approve_confirmation_scheduler_exception_marks_task_error(tmp_path):
    feedback_store = FeedbackStore(tmp_path)
    inbox_store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    task_store = TaskStore(tmp_path)
    worker_scheduler = _ExplodingScheduler()
    registry = build_builtin_command_registry()
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="发送邮件给 Alice 确认进度",
        main_session_key="main:worker-1",
    )
    confirmation = await enqueue_task_confirmation(
        inbox_store=inbox_store,
        manifest=manifest,
        task_description=manifest.task_description,
        target_session_key="main:worker-1",
        task_kind="task",
    )

    content = await registry.resolve("approve_confirmation").handler(
        _ctx(
            tmp_path,
            argv=(confirmation.inbox_id,),
            inbox_store=inbox_store,
            feedback_store=feedback_store,
            worker_scheduler=worker_scheduler,
            task_store=task_store,
        )
    )

    assert "任务提交调度失败" in content.text
    stored_task = task_store.load("tenant-1", "worker-1", manifest.task_id)
    assert stored_task is not None
    assert stored_task.status.value == "error"
    assert "scheduler offline" in stored_task.error_message
    stored_confirmation = await inbox_store.get_by_id(
        confirmation.inbox_id,
        tenant_id="tenant-1",
        worker_id="worker-1",
    )
    assert stored_confirmation is not None
    assert stored_confirmation.status == "PENDING"
    feedback = feedback_store.list_for_target(
        "tenant-1",
        "worker-1",
        target_type="task",
        target_id=manifest.task_id,
    )
    assert feedback == ()


@pytest.mark.asyncio
async def test_approve_confirmation_concurrent_requests_only_submit_once(tmp_path):
    feedback_store = FeedbackStore(tmp_path)
    inbox_store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    task_store = TaskStore(tmp_path)
    worker_scheduler = _SlowScheduler()
    registry = build_builtin_command_registry()
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="发送邮件给 Alice 确认进度",
        main_session_key="main:worker-1",
    )
    confirmation = await enqueue_task_confirmation(
        inbox_store=inbox_store,
        manifest=manifest,
        task_description=manifest.task_description,
        target_session_key="main:worker-1",
        task_kind="task",
    )

    first_task = asyncio.create_task(
        registry.resolve("approve_confirmation").handler(
            _ctx(
                tmp_path,
                argv=(confirmation.inbox_id,),
                inbox_store=inbox_store,
                feedback_store=feedback_store,
                worker_scheduler=worker_scheduler,
                task_store=task_store,
            )
        )
    )
    await worker_scheduler.started.wait()

    second = await registry.resolve("approve_confirmation").handler(
        _ctx(
            tmp_path,
            argv=(confirmation.inbox_id,),
            inbox_store=inbox_store,
            feedback_store=feedback_store,
            worker_scheduler=worker_scheduler,
            task_store=task_store,
        )
    )

    assert "not found or already handled" in second.text
    worker_scheduler.unblock.set()
    first = await first_task

    assert "已批准任务" in first.text
    assert len(worker_scheduler.jobs) == 1
    stored = await inbox_store.get_by_id(
        confirmation.inbox_id,
        tenant_id="tenant-1",
        worker_id="worker-1",
    )
    assert stored is not None
    assert stored.status == "CONSUMED"


@pytest.mark.asyncio
async def test_approve_confirmation_still_succeeds_when_consume_fails(tmp_path):
    feedback_store = FeedbackStore(tmp_path)
    inbox_store = _ExplodingConsumeInboxStore(redis_client=None, fallback_dir=tmp_path)
    task_store = TaskStore(tmp_path)
    worker_scheduler = _StubScheduler()
    registry = build_builtin_command_registry()
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="发送邮件给 Alice 确认进度",
        main_session_key="main:worker-1",
    )
    confirmation = await enqueue_task_confirmation(
        inbox_store=inbox_store,
        manifest=manifest,
        task_description=manifest.task_description,
        target_session_key="main:worker-1",
        task_kind="task",
    )

    content = await registry.resolve("approve_confirmation").handler(
        _ctx(
            tmp_path,
            argv=(confirmation.inbox_id,),
            inbox_store=inbox_store,
            feedback_store=feedback_store,
            worker_scheduler=worker_scheduler,
            task_store=task_store,
        )
    )

    assert "已批准任务" in content.text
    assert "未能标记 confirmation 已处理" in content.text
    assert len(worker_scheduler.jobs) == 1
    stored = await inbox_store.get_by_id(
        confirmation.inbox_id,
        tenant_id="tenant-1",
        worker_id="worker-1",
    )
    assert stored is not None
    assert stored.status == "PROCESSING"


@pytest.mark.asyncio
async def test_reject_confirmation_still_succeeds_when_consume_fails(tmp_path):
    feedback_store = FeedbackStore(tmp_path)
    inbox_store = _ExplodingConsumeInboxStore(redis_client=None, fallback_dir=tmp_path)
    task_store = TaskStore(tmp_path)
    registry = build_builtin_command_registry()
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="删除无效记录",
        main_session_key="main:worker-1",
    )
    confirmation = await enqueue_task_confirmation(
        inbox_store=inbox_store,
        manifest=manifest,
        task_description=manifest.task_description,
        target_session_key="main:worker-1",
        task_kind="isolated",
    )

    content = await registry.resolve("reject_confirmation").handler(
        _ctx(
            tmp_path,
            argv=(confirmation.inbox_id, "风险过高"),
            inbox_store=inbox_store,
            feedback_store=feedback_store,
            task_store=task_store,
        )
    )

    assert "已拒绝任务确认" in content.text
    assert "未能标记 confirmation 已处理" in content.text
    stored_task = task_store.load("tenant-1", "worker-1", manifest.task_id)
    assert stored_task is not None
    assert stored_task.status.value == "error"
    stored = await inbox_store.get_by_id(
        confirmation.inbox_id,
        tenant_id="tenant-1",
        worker_id="worker-1",
    )
    assert stored is not None
    assert stored.status == "PROCESSING"


@pytest.mark.asyncio
async def test_approve_confirmation_scheduler_exception_warns_when_requeue_fails(tmp_path):
    feedback_store = FeedbackStore(tmp_path)
    inbox_store = _ExplodingRequeueInboxStore(redis_client=None, fallback_dir=tmp_path)
    task_store = TaskStore(tmp_path)
    worker_scheduler = _ExplodingScheduler()
    registry = build_builtin_command_registry()
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="发送邮件给 Alice 确认进度",
        main_session_key="main:worker-1",
    )
    confirmation = await enqueue_task_confirmation(
        inbox_store=inbox_store,
        manifest=manifest,
        task_description=manifest.task_description,
        target_session_key="main:worker-1",
        task_kind="task",
    )

    content = await registry.resolve("approve_confirmation").handler(
        _ctx(
            tmp_path,
            argv=(confirmation.inbox_id,),
            inbox_store=inbox_store,
            feedback_store=feedback_store,
            worker_scheduler=worker_scheduler,
            task_store=task_store,
        )
    )

    assert "任务提交调度失败" in content.text
    assert "未能回滚 confirmation 状态" in content.text
    stored = await inbox_store.get_by_id(
        confirmation.inbox_id,
        tenant_id="tenant-1",
        worker_id="worker-1",
    )
    assert stored is not None
    assert stored.status == "PROCESSING"
