# edition: baseline
from __future__ import annotations

import asyncio

import pytest

from src.autonomy.inbox import InboxItem, SessionInboxStore
from src.autonomy.main_session import MainSessionRuntime
from src.conversation.session_manager import SessionManager
from src.conversation.session_store import FileSessionStore
from src.events.bus import EventBus
from src.worker.heartbeat.ledger import AttentionLedger
from src.worker.heartbeat.runner import HeartbeatRunner
from src.worker.scheduler import SchedulerConfig, WorkerScheduler
from src.worker.task import TaskStore


class _RecordingRouter:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def route_stream(
        self,
        task: str,
        tenant_id: str,
        worker_id: str | None = None,
        task_context: str = "",
        manifest=None,
    ):
        self.calls.append(
            {
                "task": task,
                "tenant_id": tenant_id,
                "worker_id": worker_id or "",
                "task_context": task_context,
            }
        )
        yield type(
            "Event",
            (),
            {
                "content": "task execution complete",
                "run_id": "run-task-1",
                "event_type": "TEXT_MESSAGE",
            },
        )()


@pytest.mark.asyncio
async def test_heartbeat_pipeline_escalates_goal_check_to_task(tmp_path):
    tenant_id = "demo"
    worker_id = "w1"
    event_bus = EventBus()
    router = _RecordingRouter()
    task_store = TaskStore(tmp_path)
    scheduler = WorkerScheduler(
        config=SchedulerConfig(max_concurrent_tasks=1, daily_task_quota=10),
        worker_router=router,
        event_bus=event_bus,
    )
    session_manager = SessionManager(store=FileSessionStore(tmp_path))
    main_session = MainSessionRuntime(
        session_manager=session_manager,
        tenant_id=tenant_id,
        worker_id=worker_id,
        workspace_root=tmp_path,
        redis_client=None,
    )
    await main_session.start(event_bus)

    inbox_store = SessionInboxStore(
        redis_client=None,
        fallback_dir=tmp_path,
        event_bus=event_bus,
    )
    inbox_item = await inbox_store.write(
        InboxItem(
            tenant_id=tenant_id,
            worker_id=worker_id,
            source_type="goal_check",
            event_type="goal.health_check_detected",
            dedupe_key="goal-check:1",
            payload={
                "goal_id": "goal-1",
                "goal_title": "Revenue Recovery",
                "recommended_action": "escalate",
                "deviation_score": 0.6,
            },
        )
    )

    runner = HeartbeatRunner(
        tenant_id=tenant_id,
        worker_id=worker_id,
        inbox_store=inbox_store,
        worker_router=router,
        main_session_runtime=main_session,
        attention_ledger=AttentionLedger(
            tenant_id=tenant_id,
            worker_id=worker_id,
            redis_client=None,
            workspace_root=tmp_path,
        ),
        worker_scheduler=scheduler,
        task_store=task_store,
    )

    await runner.run_once()
    await asyncio.sleep(0.1)

    manifests = task_store.list_by_worker(tenant_id, worker_id)
    assert len(manifests) == 1
    assert manifests[0].task_description.startswith("[Goal Health Follow-up]")
    assert len(router.calls) == 1
    assert router.calls[0]["task"].startswith("[Goal Health Follow-up]")

    session = await main_session.get_session()
    assert session.task_refs == (manifests[0].task_id,)

    stored = await inbox_store.get_by_id(
        inbox_item.inbox_id,
        tenant_id=tenant_id,
        worker_id=worker_id,
    )
    assert stored is not None
    assert stored.status == "CONSUMED"

    await main_session.stop()
