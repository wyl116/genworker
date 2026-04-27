# edition: baseline
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.autonomy.inbox import SessionInboxStore
from src.worker.sensing import SensorBase, SensorConfig, SensorRegistry, SnapshotStore
from src.worker.sensing.protocol import SensedFact


class _FakeScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}

    def add_job(self, func, *args, **kwargs) -> None:
        self.jobs[kwargs["id"]] = kwargs

    def remove_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)


class _PollSensor(SensorBase):
    def __init__(self) -> None:
        super().__init__(fallback_route="heartbeat")
        self.restored: dict[str, object] = {}
        self.snapshot = {"cursor": "next"}

    @property
    def sensor_type(self) -> str:
        return "workspace_file"

    @property
    def delivery_mode(self) -> str:
        return "poll"

    async def poll(self) -> tuple[SensedFact, ...]:
        return (
            SensedFact(
                source_type="workspace_file",
                event_type="local.file_changed",
                dedupe_key="file:1",
                payload=(("path", "/tmp/a.md"),),
                cognition_route="both",
            ),
        )

    def get_snapshot(self) -> dict[str, object]:
        return self.snapshot

    def restore_snapshot(self, snapshot: dict[str, object]) -> None:
        self.restored = snapshot


@pytest.mark.asyncio
async def test_sensor_registry_routes_both_to_inbox_and_event_bus(tmp_path: Path) -> None:
    inbox_store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    event_bus = AsyncMock()
    scheduler = _FakeScheduler()
    snapshot_store = SnapshotStore(tmp_path, "demo", "worker-1")
    await snapshot_store.save("workspace_file", {"cursor": "prev"})

    registry = SensorRegistry(
        tenant_id="demo",
        worker_id="worker-1",
        inbox_store=inbox_store,
        event_bus=event_bus,
        scheduler=scheduler,
        snapshot_store=snapshot_store,
    )
    sensor = _PollSensor()

    await registry.register(sensor, SensorConfig(source_type="workspace_file", poll_interval="1m"))
    assert sensor.restored == {"cursor": "prev"}
    assert "sensor:worker-1:workspace_file" in scheduler.jobs

    await registry.on_facts_sensed(await sensor.poll(), sensor.sensor_type)

    pending = await inbox_store.fetch_pending(tenant_id="demo", worker_id="worker-1")
    assert len(pending) == 1
    assert pending[0].event_type == "local.file_changed"
    event_bus.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_sensor_registry_override_forces_route(tmp_path: Path) -> None:
    inbox_store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    event_bus = AsyncMock()
    registry = SensorRegistry(
        tenant_id="demo",
        worker_id="worker-1",
        inbox_store=inbox_store,
        event_bus=event_bus,
        scheduler=_FakeScheduler(),
        snapshot_store=SnapshotStore(tmp_path, "demo", "worker-1"),
        cognition_route_overrides={"workspace_file": "reactive"},
    )

    await registry.on_facts_sensed((
        SensedFact(
            source_type="workspace_file",
            event_type="local.file_changed",
            dedupe_key="file:1",
            payload=(("path", "/tmp/a.md"),),
            cognition_route="heartbeat",
        ),
    ), "workspace_file")

    pending = await inbox_store.fetch_pending(tenant_id="demo", worker_id="worker-1")
    assert pending == ()
    event_bus.publish.assert_awaited_once()
