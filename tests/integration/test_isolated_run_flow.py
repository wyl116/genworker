# edition: baseline
from __future__ import annotations

import asyncio

import pytest

from src.autonomy.inbox import InboxItem, SessionInboxStore
from src.autonomy.isolated_run import IsolatedRunManager
from src.autonomy.main_session import MainSessionRuntime
from src.conversation.session_manager import SessionManager
from src.conversation.session_store import FileSessionStore
from src.events.bus import EventBus
from src.worker.heartbeat.ledger import AttentionLedger
from src.worker.heartbeat.runner import HeartbeatRunner
from src.worker.scheduler import SchedulerConfig, WorkerScheduler
from src.worker.task import TaskStore


class _IsolatedRouter:
    async def route_stream(
        self,
        task: str,
        tenant_id: str,
        worker_id: str | None = None,
        task_context: str = "",
        manifest=None,
    ):
        yield type(
            "Event",
            (),
            {
                "content": "deep analysis result",
                "run_id": "run-iso-1",
                "event_type": "TEXT_MESSAGE",
            },
        )()


@pytest.mark.asyncio
async def test_heartbeat_isolated_run_flows_back_to_main_session(tmp_path):
    tenant_id = "demo"
    worker_id = "w1"
    event_bus = EventBus()
    router = _IsolatedRouter()
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
    await inbox_store.write(
        InboxItem(
            tenant_id=tenant_id,
            worker_id=worker_id,
            source_type="goal_check",
            event_type="goal.health_check_detected",
            dedupe_key="goal-check-iso:1",
            payload={
                "goal_id": "goal-2",
                "goal_title": "Critical Incident",
                "recommended_action": "replan",
                "deviation_score": 0.97,
            },
        )
    )

    isolated_run_manager = IsolatedRunManager(
        task_store=task_store,
        worker_schedulers={worker_id: scheduler},
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
        isolated_run_manager=isolated_run_manager,
    )

    await runner.run_once()
    await asyncio.sleep(0.2)

    session = await main_session.get_session()
    assert session.messages
    assert session.messages[-1].content.startswith("[IsolatedRun run-iso-1 完成]")
    assert "deep analysis result" in session.messages[-1].content

    manifests = task_store.list_by_worker(tenant_id, worker_id)
    assert len(manifests) == 1
    assert manifests[0].main_session_key == "main:w1"
    assert session.task_refs == (manifests[0].task_id,)

    await main_session.stop()
