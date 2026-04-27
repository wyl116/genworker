"""Bootstrap initializer for per-worker sensor registries."""
from __future__ import annotations

from pathlib import Path

from src.common.logger import get_logger
from src.worker.sensing import SensorRegistry, SnapshotStore, parse_sensor_config
from src.worker.sensing.factory import create_sensor

from .base import Initializer

logger = get_logger()


class SensorInitializer(Initializer):
    """Create per-worker sensor registries."""

    def __init__(self) -> None:
        self._registries: dict[str, SensorRegistry] = {}

    @property
    def name(self) -> str:
        return "sensors"

    @property
    def depends_on(self) -> list[str]:
        return ["scheduler", "events", "platforms", "integrations"]

    @property
    def priority(self) -> int:
        return 132

    @property
    def required(self) -> bool:
        return False

    async def initialize(self, context) -> bool:
        worker_registry = context.get_state("worker_registry")
        if worker_registry is None:
            context.set_state("sensor_registries", {})
            return True

        tenant_id = context.get_state("tenant_id", "demo")
        workspace_root = Path(context.get_state("workspace_root", "workspace"))
        inbox_store = (
            context.get_state("session_inbox_store")
            or context.get_state("integration_inbox_store")
        )
        deps = {
            "tenant_id": tenant_id,
            "platform_client_factory": context.get_state("platform_client_factory"),
            "redis_client": context.get_state("redis_client"),
            "tool_executor": context.get_state("tool_executor"),
            "mount_manager": context.get_state("mount_manager"),
            "message_deduplicator": context.get_state("message_deduplicator"),
        }

        for entry in worker_registry.list_all():
            worker = entry.worker
            if not worker.sensor_configs:
                continue

            overrides: dict[str, str] = {}
            for raw in worker.sensor_configs:
                override = str(raw.get("cognition_route_override", "")).strip()
                if override:
                    overrides[str(raw.get("source_type", ""))] = override

            registry = SensorRegistry(
                tenant_id=tenant_id,
                worker_id=worker.worker_id,
                inbox_store=inbox_store,
                event_bus=context.get_state("event_bus"),
                scheduler=context.get_state("apscheduler"),
                snapshot_store=SnapshotStore(
                    workspace_root=workspace_root,
                    tenant_id=tenant_id,
                    worker_id=worker.worker_id,
                ),
                cognition_route_overrides=overrides or None,
            )

            for raw in worker.sensor_configs:
                config = parse_sensor_config(raw)
                try:
                    sensor = create_sensor(
                        config,
                        worker_id=worker.worker_id,
                        **deps,
                    )
                except ValueError as exc:
                    logger.warning(
                        "[SensorInit] Unsupported sensor '%s' for worker '%s': %s",
                        config.source_type,
                        worker.worker_id,
                        exc,
                    )
                    continue
                await registry.register(sensor, config)

            self._registries[worker.worker_id] = registry

        context.set_state("sensor_registries", dict(self._registries))
        channel_router = context.get_state("channel_message_router")
        if channel_router is not None and hasattr(channel_router, "replace_sensor_registries"):
            channel_router.replace_sensor_registries(dict(self._registries))
        logger.info(
            "[SensorInit] Registered sensor registries for %s workers",
            len(self._registries),
        )
        return True

    async def cleanup(self) -> None:
        for registry in self._registries.values():
            await registry.stop_all()
        self._registries.clear()
