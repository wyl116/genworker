# edition: baseline
from types import SimpleNamespace

import pytest

from src.autonomy.inbox import SessionInboxStore
from src.bootstrap.context import BootstrapContext
from src.bootstrap.heartbeat_init import HeartbeatInitializer
from src.common.runtime_status import ComponentStatus


class _EmptyWorkerRegistry:
    def list_all(self):
        return []


@pytest.mark.asyncio
async def test_heartbeat_init_reuses_existing_goal_inbox_store(tmp_path):
    existing_store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    context = BootstrapContext(
        settings=SimpleNamespace(
            heartbeat_processing_timeout_minutes=10,
            heartbeat_interval_minutes=5,
        )
    )
    context.set_state("apscheduler", None)
    context.set_state("worker_router", object())
    context.set_state("worker_registry", _EmptyWorkerRegistry())
    context.set_state("session_manager", object())
    context.set_state("task_store", None)
    context.set_state("worker_schedulers", {})
    context.set_state("event_bus", None)
    context.set_state("tenant_id", "demo")
    context.set_state("redis_client", None)
    context.set_state("workspace_root", tmp_path)
    context.set_state("goal_inbox_store", existing_store)

    init = HeartbeatInitializer()
    ok = await init.initialize(context)

    assert ok is True
    assert context.get_state("session_inbox_store") is existing_store


@pytest.mark.asyncio
async def test_heartbeat_init_registers_runtime_components_even_without_workers(tmp_path):
    context = BootstrapContext(
        settings=SimpleNamespace(
            heartbeat_processing_timeout_minutes=10,
            heartbeat_interval_minutes=5,
        )
    )
    context.set_state("apscheduler", None)
    context.set_state("worker_router", object())
    context.set_state("worker_registry", _EmptyWorkerRegistry())
    context.set_state("session_manager", object())
    context.set_state("task_store", None)
    context.set_state("worker_schedulers", {})
    context.set_state("event_bus", None)
    context.set_state("tenant_id", "demo")
    context.set_state("redis_client", None)
    context.set_state("workspace_root", tmp_path)

    init = HeartbeatInitializer()
    ok = await init.initialize(context)
    snapshot = context.snapshot_runtime_components()

    assert ok is True
    assert snapshot["main_session_meta"].status == ComponentStatus.DISABLED
    assert snapshot["attention_ledger"].status == ComponentStatus.DISABLED
