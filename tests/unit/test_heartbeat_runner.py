# edition: baseline
import pytest

from src.autonomy.inbox import InboxItem, SessionInboxStore
from src.autonomy.main_session import MainSessionRuntime
from src.conversation.session_manager import SessionManager
from src.conversation.session_store import FileSessionStore
from src.worker.lifecycle.task_confirmation import CONFIRMATION_EVENT_TYPE
from src.worker.heartbeat.ledger import AttentionLedger
from src.worker.heartbeat.runner import HeartbeatRunner
from src.worker.scripts.models import InlineScript, serialize_pre_script


class _FakeRouter:
    def __init__(self) -> None:
        self.tasks = []

    async def route_stream(self, task, tenant_id, worker_id=None, task_context=""):
        self.tasks.append(task)
        yield type(
            "Event",
            (),
            {"content": "heartbeat summary", "event_type": "TEXT_MESSAGE"},
        )()


class _FakeScheduler:
    def __init__(self) -> None:
        self.jobs = []

    async def submit_task(self, job, priority):
        self.jobs.append((job, priority))
        return True


class _RejectingScheduler:
    def __init__(self) -> None:
        self.jobs = []

    async def submit_task(self, job, priority):
        self.jobs.append((job, priority))
        return False


class _FakeIsolatedRunManager:
    def __init__(self) -> None:
        self.calls = []

    async def create_run(
        self,
        *,
        tenant_id,
        worker_id,
        task_description,
        main_session_key,
        preferred_skill_ids=(),
        provenance=None,
        pre_script=None,
        gate_level="gated",
    ):
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "worker_id": worker_id,
                "task_description": task_description,
                "main_session_key": main_session_key,
                "preferred_skill_ids": tuple(preferred_skill_ids),
                "provenance": provenance,
                "pre_script": pre_script,
                "gate_level": gate_level,
            }
        )
        return type("Manifest", (), {"task_id": "isolated-1"})()


@pytest.mark.asyncio
async def test_heartbeat_runner_consumes_inbox_and_appends_summary(tmp_path):
    session_manager = SessionManager(store=FileSessionStore(tmp_path))
    runtime = MainSessionRuntime(
        session_manager=session_manager,
        tenant_id="demo",
        worker_id="w1",
        workspace_root=tmp_path,
        redis_client=None,
    )
    store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    item = InboxItem(
        tenant_id="demo",
        worker_id="w1",
        source_type="email",
        event_type="external.email_received",
        dedupe_key="email:1",
        payload={"subject": "hello"},
    )
    await store.write(item)

    runner = HeartbeatRunner(
        tenant_id="demo",
        worker_id="w1",
        inbox_store=store,
        worker_router=_FakeRouter(),
        main_session_runtime=runtime,
        attention_ledger=AttentionLedger(
            tenant_id="demo",
            worker_id="w1",
            redis_client=None,
            workspace_root=tmp_path,
        ),
    )

    await runner.run_once()

    session = await runtime.get_session()
    stored = await store.get_by_id(item.inbox_id, tenant_id="demo", worker_id="w1")
    assert session.messages[-1].content == "heartbeat summary"
    assert stored is not None
    assert stored.status == "CONSUMED"


@pytest.mark.asyncio
async def test_goal_check_creates_direct_task(tmp_path):
    session_manager = SessionManager(store=FileSessionStore(tmp_path))
    runtime = MainSessionRuntime(
        session_manager=session_manager,
        tenant_id="demo",
        worker_id="w1",
        workspace_root=tmp_path,
        redis_client=None,
    )
    store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    scheduler = _FakeScheduler()
    router = _FakeRouter()
    item = InboxItem(
        tenant_id="demo",
        worker_id="w1",
        source_type="goal_check",
        event_type="goal.health_check_detected",
        dedupe_key="goal:1",
        payload={
            "goal_id": "goal-1",
            "goal_title": "Important Goal",
            "recommended_action": "escalate",
            "deviation_score": 0.7,
            "pre_script": serialize_pre_script(InlineScript(source="print('goal prefetch')")),
        },
    )
    await store.write(item)

    runner = HeartbeatRunner(
        tenant_id="demo",
        worker_id="w1",
        inbox_store=store,
        worker_router=router,
        main_session_runtime=runtime,
        attention_ledger=AttentionLedger(
            tenant_id="demo",
            worker_id="w1",
            redis_client=None,
            workspace_root=tmp_path,
        ),
        worker_scheduler=scheduler,
    )

    await runner.run_once()

    assert len(scheduler.jobs) == 1
    job, priority = scheduler.jobs[0]
    assert "[Goal Health Follow-up] Important Goal" in job["task"]
    assert isinstance(job["manifest"].pre_script, InlineScript)
    assert job["manifest"].pre_script.source.strip() == "print('goal prefetch')"
    assert priority > 0
    assert job["session_id"]
    assert job["thread_id"] == "main:w1"
    assert job["main_session_key"] == "main:w1"
    assert router.tasks == []


@pytest.mark.asyncio
async def test_goal_check_can_escalate_to_isolated_run(tmp_path):
    session_manager = SessionManager(store=FileSessionStore(tmp_path))
    runtime = MainSessionRuntime(
        session_manager=session_manager,
        tenant_id="demo",
        worker_id="w1",
        workspace_root=tmp_path,
        redis_client=None,
    )
    store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    isolated_run_manager = _FakeIsolatedRunManager()
    item = InboxItem(
        tenant_id="demo",
        worker_id="w1",
        source_type="goal_check",
        event_type="goal.health_check_detected",
        dedupe_key="goal:2",
        payload={
            "goal_id": "goal-2",
            "goal_title": "Critical Goal",
            "recommended_action": "replan",
            "deviation_score": 0.95,
            "pre_script": serialize_pre_script(InlineScript(source="print('isolated prefetch')")),
        },
    )
    await store.write(item)

    runner = HeartbeatRunner(
        tenant_id="demo",
        worker_id="w1",
        inbox_store=store,
        worker_router=_FakeRouter(),
        main_session_runtime=runtime,
        attention_ledger=AttentionLedger(
            tenant_id="demo",
            worker_id="w1",
            redis_client=None,
            workspace_root=tmp_path,
        ),
        isolated_run_manager=isolated_run_manager,
    )

    await runner.run_once()

    assert len(isolated_run_manager.calls) == 1
    assert "[Goal Health Follow-up] Critical Goal" in isolated_run_manager.calls[0]["task_description"]
    assert isolated_run_manager.calls[0]["preferred_skill_ids"] == ()
    assert isinstance(isolated_run_manager.calls[0]["pre_script"], InlineScript)
    assert isolated_run_manager.calls[0]["pre_script"].source.strip() == "print('isolated prefetch')"


@pytest.mark.asyncio
async def test_goal_check_scheduler_rejection_marks_task_error(tmp_path):
    from src.worker.task import TaskStore

    session_manager = SessionManager(store=FileSessionStore(tmp_path))
    runtime = MainSessionRuntime(
        session_manager=session_manager,
        tenant_id="demo",
        worker_id="w1",
        workspace_root=tmp_path,
        redis_client=None,
    )
    store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    scheduler = _RejectingScheduler()
    task_store = TaskStore(tmp_path)
    item = InboxItem(
        tenant_id="demo",
        worker_id="w1",
        source_type="goal_check",
        event_type="goal.health_check_detected",
        dedupe_key="goal:reject",
        payload={
            "goal_id": "goal-3",
            "goal_title": "Rejected Goal",
            "recommended_action": "escalate",
            "deviation_score": 0.7,
        },
    )
    await store.write(item)

    runner = HeartbeatRunner(
        tenant_id="demo",
        worker_id="w1",
        inbox_store=store,
        worker_router=_FakeRouter(),
        main_session_runtime=runtime,
        attention_ledger=AttentionLedger(
            tenant_id="demo",
            worker_id="w1",
            redis_client=None,
            workspace_root=tmp_path,
        ),
        worker_scheduler=scheduler,
        task_store=task_store,
    )

    await runner.run_once()

    manifests = task_store.list_by_worker("demo", "w1")
    assert len(manifests) == 1
    assert manifests[0].status.value == "error"
    assert manifests[0].error_message == "Scheduler quota exhausted"
    session = await runtime.get_session()
    assert session.task_refs == ()


@pytest.mark.asyncio
async def test_goal_check_missing_scheduler_records_error_and_session_notice(tmp_path):
    from src.worker.task import TaskStore

    session_manager = SessionManager(store=FileSessionStore(tmp_path))
    runtime = MainSessionRuntime(
        session_manager=session_manager,
        tenant_id="demo",
        worker_id="w1",
        workspace_root=tmp_path,
        redis_client=None,
    )
    store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    task_store = TaskStore(tmp_path)
    item = InboxItem(
        tenant_id="demo",
        worker_id="w1",
        source_type="goal_check",
        event_type="goal.health_check_detected",
        dedupe_key="goal:no-scheduler",
        payload={
            "goal_id": "goal-4",
            "goal_title": "No Scheduler Goal",
            "recommended_action": "escalate",
            "deviation_score": 0.7,
        },
    )
    await store.write(item)

    runner = HeartbeatRunner(
        tenant_id="demo",
        worker_id="w1",
        inbox_store=store,
        worker_router=_FakeRouter(),
        main_session_runtime=runtime,
        attention_ledger=AttentionLedger(
            tenant_id="demo",
            worker_id="w1",
            redis_client=None,
            workspace_root=tmp_path,
        ),
        task_store=task_store,
    )

    await runner.run_once()

    manifests = task_store.list_by_worker("demo", "w1")
    assert len(manifests) == 1
    assert manifests[0].status.value == "error"
    assert manifests[0].error_message == "Worker scheduler is not available"
    session = await runtime.get_session()
    assert session.task_refs == ()
    assert "worker scheduler 不可用" in session.messages[-1].content


@pytest.mark.asyncio
async def test_email_followup_can_create_task_without_explicit_prompt(tmp_path):
    session_manager = SessionManager(store=FileSessionStore(tmp_path))
    runtime = MainSessionRuntime(
        session_manager=session_manager,
        tenant_id="demo",
        worker_id="w1",
        workspace_root=tmp_path,
        redis_client=None,
    )
    store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    scheduler = _FakeScheduler()
    router = _FakeRouter()
    item = InboxItem(
        tenant_id="demo",
        worker_id="w1",
        source_type="email",
        event_type="external.email_received",
        dedupe_key="email:2",
        payload={
            "subject": "Need action",
            "from": "alice@example.com",
            "content": "please follow up",
            "requires_follow_up": True,
        },
    )
    await store.write(item)

    runner = HeartbeatRunner(
        tenant_id="demo",
        worker_id="w1",
        inbox_store=store,
        worker_router=router,
        main_session_runtime=runtime,
        attention_ledger=AttentionLedger(
            tenant_id="demo",
            worker_id="w1",
            redis_client=None,
            workspace_root=tmp_path,
        ),
        worker_scheduler=scheduler,
    )

    await runner.run_once()

    assert len(scheduler.jobs) == 1
    assert "[Email Follow-up] Need action" in scheduler.jobs[0][0]["task"]
    assert router.tasks == []


@pytest.mark.asyncio
async def test_gated_task_becomes_confirmation_request(tmp_path):
    session_manager = SessionManager(store=FileSessionStore(tmp_path))
    runtime = MainSessionRuntime(
        session_manager=session_manager,
        tenant_id="demo",
        worker_id="w1",
        workspace_root=tmp_path,
        redis_client=None,
    )
    store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    scheduler = _FakeScheduler()
    item = InboxItem(
        tenant_id="demo",
        worker_id="w1",
        source_type="email",
        event_type="external.email_received",
        dedupe_key="email:confirm:1",
        payload={
            "run_mode": "task",
            "task_description": "发送邮件给 Alice 确认进度",
        },
    )
    await store.write(item)

    runner = HeartbeatRunner(
        tenant_id="demo",
        worker_id="w1",
        inbox_store=store,
        worker_router=_FakeRouter(),
        main_session_runtime=runtime,
        attention_ledger=AttentionLedger(
            tenant_id="demo",
            worker_id="w1",
            redis_client=None,
            workspace_root=tmp_path,
        ),
        worker_scheduler=scheduler,
    )

    await runner.run_once()

    assert scheduler.jobs == []
    confirmations = await store.list_pending(
        tenant_id="demo",
        worker_id="w1",
        event_type=CONFIRMATION_EVENT_TYPE,
    )
    assert len(confirmations) == 1
    assert confirmations[0].payload["task_description"] == "发送邮件给 Alice 确认进度"
    original = await store.get_by_id(item.inbox_id, tenant_id="demo", worker_id="w1")
    assert original is not None
    assert original.status == "CONSUMED"


@pytest.mark.asyncio
async def test_gated_isolated_task_becomes_confirmation_request(tmp_path):
    session_manager = SessionManager(store=FileSessionStore(tmp_path))
    runtime = MainSessionRuntime(
        session_manager=session_manager,
        tenant_id="demo",
        worker_id="w1",
        workspace_root=tmp_path,
        redis_client=None,
    )
    store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    isolated_run_manager = _FakeIsolatedRunManager()
    item = InboxItem(
        tenant_id="demo",
        worker_id="w1",
        source_type="email",
        event_type="external.email_received",
        dedupe_key="email:confirm:2",
        payload={
            "run_mode": "isolated",
            "task_description": "删除临时目录中的过期文件",
        },
    )
    await store.write(item)

    runner = HeartbeatRunner(
        tenant_id="demo",
        worker_id="w1",
        inbox_store=store,
        worker_router=_FakeRouter(),
        main_session_runtime=runtime,
        attention_ledger=AttentionLedger(
            tenant_id="demo",
            worker_id="w1",
            redis_client=None,
            workspace_root=tmp_path,
        ),
        isolated_run_manager=isolated_run_manager,
    )

    await runner.run_once()

    assert isolated_run_manager.calls == []
    confirmations = await store.list_pending(
        tenant_id="demo",
        worker_id="w1",
        event_type=CONFIRMATION_EVENT_TYPE,
    )
    assert len(confirmations) == 1
    assert confirmations[0].payload["task_kind"] == "isolated"
