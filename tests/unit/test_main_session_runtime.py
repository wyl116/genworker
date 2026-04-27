# edition: baseline
import pytest

from src.autonomy.main_session import MainSessionRuntime
from src.conversation.session_manager import SessionManager
from src.conversation.session_store import FileSessionStore
from src.events.bus import EventBus
from src.events.models import Event


class _FailingRedis:
    async def get(self, *args, **kwargs):
        raise RuntimeError("redis down")

    async def get_json(self, *args, **kwargs):
        raise RuntimeError("redis down")

    async def smembers(self, *args, **kwargs):
        raise RuntimeError("redis down")

    async def set(self, *args, **kwargs):
        raise RuntimeError("redis down")

    async def set_json(self, *args, **kwargs):
        raise RuntimeError("redis down")

    async def delete(self, *args, **kwargs):
        raise RuntimeError("redis down")

    async def sadd(self, *args, **kwargs):
        raise RuntimeError("redis down")


@pytest.mark.asyncio
async def test_main_session_runtime_handles_isolated_run_events(tmp_path):
    manager = SessionManager(store=FileSessionStore(tmp_path))
    runtime = MainSessionRuntime(
        session_manager=manager,
        tenant_id="demo",
        worker_id="w1",
        workspace_root=tmp_path,
        redis_client=None,
    )
    event_bus = EventBus()
    await runtime.start(event_bus)

    await event_bus.publish(
        Event(
            event_id="evt-1",
            type="isolated_run.completed",
            source="test",
            tenant_id="demo",
            payload=(
                ("main_session_key", "main:w1"),
                ("run_id", "run-1"),
                ("summary", "analysis finished"),
            ),
        )
    )

    session = await runtime.get_session()
    assert session.session_type == "main"
    assert session.main_session_key == "main:w1"
    assert session.messages[-1].content == "[IsolatedRun run-1 完成] analysis finished"

    await runtime.stop()
    assert event_bus.subscription_count == 0


@pytest.mark.asyncio
async def test_main_session_runtime_handles_direct_task_events(tmp_path):
    manager = SessionManager(store=FileSessionStore(tmp_path))
    runtime = MainSessionRuntime(
        session_manager=manager,
        tenant_id="demo",
        worker_id="w1",
        workspace_root=tmp_path,
        redis_client=None,
    )
    event_bus = EventBus()
    await runtime.start(event_bus)

    await event_bus.publish(
        Event(
            event_id="evt-task-completed",
            type="task.completed",
            source="test",
            tenant_id="demo",
            payload=(
                ("task_id", "task-1"),
                ("description", "整理日报"),
                ("summary", "日报已整理并归档"),
                ("thread_id", "main:w1"),
            ),
        )
    )
    await event_bus.publish(
        Event(
            event_id="evt-task-failed",
            type="task.failed",
            source="test",
            tenant_id="demo",
            payload=(
                ("task_id", "task-2"),
                ("thread_id", "main:w1"),
                ("error_message", "quota exhausted"),
            ),
        )
    )
    await event_bus.publish(
        Event(
            event_id="evt-task-other",
            type="task.completed",
            source="test",
            tenant_id="demo",
            payload=(
                ("task_id", "task-3"),
                ("description", "should ignore"),
                ("thread_id", "main:other"),
            ),
        )
    )

    session = await runtime.get_session()
    contents = [message.content for message in session.messages]
    assert "[Task 完成] 整理日报\n日报已整理并归档" in contents
    assert "[Task 失败] task-2: quota exhausted" in contents
    assert all("should ignore" not in content for content in contents)

    await runtime.stop()
    assert event_bus.subscription_count == 0


@pytest.mark.asyncio
async def test_main_session_runtime_handles_isolated_run_failed_without_run_id(tmp_path):
    manager = SessionManager(store=FileSessionStore(tmp_path))
    runtime = MainSessionRuntime(
        session_manager=manager,
        tenant_id="demo",
        worker_id="w1",
        workspace_root=tmp_path,
        redis_client=None,
    )
    event_bus = EventBus()
    await runtime.start(event_bus)

    await event_bus.publish(
        Event(
            event_id="evt-iso-failed-no-run",
            type="isolated_run.failed",
            source="test",
            tenant_id="demo",
            payload=(
                ("main_session_key", "main:w1"),
                ("task_id", "task-iso-1"),
                ("error_message", "Scheduler quota exhausted"),
            ),
        )
    )

    session = await runtime.get_session()
    assert session.messages[-1].content == "[IsolatedRun task-iso-1 失败] Scheduler quota exhausted"

    await runtime.stop()
    assert event_bus.subscription_count == 0


@pytest.mark.asyncio
async def test_main_session_runtime_runtime_status_degrades_on_redis_fallback(tmp_path):
    manager = SessionManager(store=FileSessionStore(tmp_path))
    runtime = MainSessionRuntime(
        session_manager=manager,
        tenant_id="demo",
        worker_id="w1",
        workspace_root=tmp_path,
        redis_client=_FailingRedis(),
    )

    await runtime.update_heartbeat_state(
        inbox_cursor="cursor-1",
        open_concerns=("risk",),
        task_refs=("task-1",),
    )

    status = runtime.runtime_status()
    assert status.status.value == "degraded"
    assert status.selected_backend == "file"
