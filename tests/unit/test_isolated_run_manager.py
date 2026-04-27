# edition: baseline
import pytest

from src.autonomy.isolated_run import IsolatedRunManager
from src.events.bus import EventBus, Subscription
from src.worker.scripts.models import InlineScript
from src.worker.task import TaskStore


class _FakeScheduler:
    def __init__(self) -> None:
        self.jobs = []

    async def submit_task(self, job, priority):
        self.jobs.append((job, priority))
        return True


class _RejectingScheduler:
    async def submit_task(self, job, priority):
        return False


@pytest.mark.asyncio
async def test_isolated_run_manager_submits_with_main_session_key(tmp_path):
    scheduler = _FakeScheduler()
    manager = IsolatedRunManager(
        task_store=TaskStore(tmp_path),
        worker_schedulers={"w1": scheduler},
    )

    manifest = await manager.create_run(
        tenant_id="demo",
        worker_id="w1",
        task_description="deep analysis",
        main_session_key="main:w1",
        pre_script=InlineScript(source="print('prefetch')"),
    )

    assert manifest.main_session_key == "main:w1"
    assert isinstance(manifest.pre_script, InlineScript)
    assert scheduler.jobs[0][0]["main_session_key"] == "main:w1"
    assert scheduler.jobs[0][0]["manifest"].main_session_key == "main:w1"
    assert scheduler.jobs[0][0]["manifest"].pre_script == manifest.pre_script


@pytest.mark.asyncio
async def test_isolated_run_manager_marks_error_when_scheduler_rejects(tmp_path):
    manager = IsolatedRunManager(
        task_store=TaskStore(tmp_path),
        worker_schedulers={"w1": _RejectingScheduler()},
    )

    manifest = await manager.create_run(
        tenant_id="demo",
        worker_id="w1",
        task_description="deep analysis",
        main_session_key="main:w1",
    )

    assert manifest.status.value == "error"
    assert manifest.error_message == "Scheduler quota exhausted"


@pytest.mark.asyncio
async def test_isolated_run_manager_publishes_failed_event_on_scheduler_rejection(tmp_path):
    captured = []

    async def _on_failed(event):
        captured.append(dict(event.payload))

    event_bus = EventBus()
    event_bus.subscribe(Subscription(
        handler_id="test-isolated-run-failed",
        event_type="isolated_run.failed",
        tenant_id="demo",
        handler=_on_failed,
    ))
    manager = IsolatedRunManager(
        task_store=TaskStore(tmp_path),
        worker_schedulers={"w1": _RejectingScheduler()},
        event_bus=event_bus,
    )

    manifest = await manager.create_run(
        tenant_id="demo",
        worker_id="w1",
        task_description="deep analysis",
        main_session_key="main:w1",
    )

    assert manifest.status.value == "error"
    assert captured
    assert captured[0]["main_session_key"] == "main:w1"
    assert captured[0]["task_id"] == manifest.task_id
    assert captured[0]["error_message"] == "Scheduler quota exhausted"
