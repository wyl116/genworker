# edition: baseline
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.bootstrap.context import BootstrapContext
from src.bootstrap.sensor_init import SensorInitializer
from src.events.bus import EventBus
from src.skills.registry import SkillRegistry
from src.worker.models import Worker, WorkerIdentity
from src.worker.registry import WorkerEntry, build_worker_registry


class _FakeScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}

    def add_job(self, func, *args, **kwargs) -> None:
        self.jobs[kwargs["id"]] = kwargs

    def remove_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)


@pytest.mark.asyncio
async def test_sensor_initializer_builds_workspace_sensor_registry(tmp_path) -> None:
    worker = Worker(
        identity=WorkerIdentity(worker_id="worker-1", name="Worker 1", role="analyst"),
        sensor_configs=(
            {
                "source_type": "workspace_file",
                "poll_interval": "1m",
                "filter": {
                    "watch_paths": str(tmp_path),
                    "patterns": "*.md",
                },
            },
        ),
    )
    registry = build_worker_registry(
        [WorkerEntry(worker=worker, skill_registry=SkillRegistry.from_skills([]))],
        default_worker_id="worker-1",
    )

    context = BootstrapContext(settings=SimpleNamespace())
    context.set_state("tenant_id", "demo")
    context.set_state("workspace_root", tmp_path)
    context.set_state("worker_registry", registry)
    context.set_state("event_bus", EventBus())
    context.set_state("apscheduler", _FakeScheduler())
    context.set_state("integration_inbox_store", None)

    init = SensorInitializer()
    result = await init.initialize(context)

    registries = context.get_state("sensor_registries")
    assert result is True
    assert "worker-1" in registries
    assert registries["worker-1"].get_sensor("workspace_file") is not None
    await init.cleanup()
