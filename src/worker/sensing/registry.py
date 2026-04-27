"""Sensor registry coordinating lifecycle and fact routing."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from src.autonomy.inbox import InboxItem, SessionInboxStore
from src.common.logger import get_logger
from src.events.models import Event, EventBusProtocol

from .config import SensorConfig
from .protocol import Sensor, SensedFact

logger = get_logger()

_VALID_ROUTES = frozenset({"heartbeat", "reactive", "both"})


def _parse_interval(interval_str: str) -> int:
    interval = str(interval_str or "5m").strip().lower()
    if interval.endswith("h"):
        return int(interval[:-1]) * 3600
    if interval.endswith("m"):
        return int(interval[:-1]) * 60
    if interval.endswith("s"):
        return int(interval[:-1])
    return int(interval)


class SensorRegistry:
    """Central coordinator for sensors attached to one worker."""

    def __init__(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        inbox_store: SessionInboxStore | None,
        event_bus: EventBusProtocol | None,
        scheduler: Any,
        snapshot_store,
        cognition_route_overrides: dict[str, str] | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._worker_id = worker_id
        self._inbox_store = inbox_store
        self._event_bus = event_bus
        self._scheduler = scheduler
        self._snapshot_store = snapshot_store
        self._overrides = cognition_route_overrides or {}
        self._sensors: dict[str, Sensor] = {}
        self._sensor_modes: dict[str, str] = {}
        self._configs: dict[str, SensorConfig] = {}

    async def register(self, sensor: Sensor, config: SensorConfig) -> None:
        snapshot = await self._snapshot_store.load(sensor.sensor_type)
        if snapshot:
            sensor.restore_snapshot(snapshot)

        effective_mode = str(config.delivery_mode or sensor.delivery_mode or "poll")
        if effective_mode == "push":
            sensor.set_fact_callback(
                lambda facts, sensor_type=sensor.sensor_type: self.on_facts_sensed(
                    facts, sensor_type,
                )
            )
            await sensor.start()
        elif effective_mode == "poll":
            interval = _parse_interval(config.poll_interval)
            if self._scheduler is None:
                raise RuntimeError("Scheduler is required for poll-mode sensors")
            self._scheduler.add_job(
                self._poll_and_route,
                "interval",
                seconds=interval,
                args=[sensor],
                id=self._job_id(sensor.sensor_type),
                replace_existing=True,
            )
        else:
            raise ValueError(f"Unknown delivery_mode: '{effective_mode}'")

        self._sensors[sensor.sensor_type] = sensor
        self._sensor_modes[sensor.sensor_type] = effective_mode
        self._configs[sensor.sensor_type] = config

    async def unregister(self, sensor_type: str) -> None:
        sensor = self._sensors.pop(sensor_type, None)
        mode = self._sensor_modes.pop(sensor_type, "")
        self._configs.pop(sensor_type, None)
        if sensor is None:
            return

        if mode == "poll" and self._scheduler is not None:
            try:
                self._scheduler.remove_job(self._job_id(sensor_type))
            except Exception:
                pass

        await self._snapshot_store.save(sensor_type, sensor.get_snapshot())
        await sensor.stop()

    async def on_facts_sensed(
        self,
        facts: tuple[SensedFact, ...],
        sensor_type: str,
    ) -> None:
        if not facts:
            return

        for fact in facts:
            route = self._resolve_route(fact)
            if route in ("heartbeat", "both") and self._inbox_store is not None:
                await self._inbox_store.write(
                    InboxItem(
                        tenant_id=self._tenant_id,
                        worker_id=self._worker_id,
                        target_session_key=f"main:{self._worker_id}",
                        source_type=fact.source_type,
                        event_type=fact.event_type,
                        priority_hint=fact.priority_hint,
                        dedupe_key=fact.dedupe_key,
                        payload=fact.payload_dict,
                    )
                )

            if route in ("reactive", "both") and self._event_bus is not None:
                await self._event_bus.publish(
                    Event(
                        event_id=f"evt-{uuid4().hex[:8]}",
                        type=fact.event_type,
                        source=f"sensor:{sensor_type}",
                        tenant_id=self._tenant_id,
                        payload=self._with_worker_id(fact.payload),
                    )
                )

        sensor = self._sensors.get(sensor_type)
        if sensor is not None:
            await self._snapshot_store.save(sensor_type, sensor.get_snapshot())

    def get_sensor(self, sensor_type: str) -> Sensor | None:
        return self._sensors.get(sensor_type)

    async def stop_all(self) -> None:
        for sensor_type in tuple(self._sensors.keys()):
            await self.unregister(sensor_type)

    @property
    def health(self) -> dict[str, Any]:
        return {
            "sensor_count": len(self._sensors),
            "sensors": {
                sensor_type: {
                    "delivery_mode": self._sensor_modes.get(sensor_type, ""),
                    "config": {
                        "poll_interval": self._configs.get(sensor_type, SensorConfig(source_type=sensor_type)).poll_interval,
                    },
                }
                for sensor_type in self._sensors
            },
        }

    async def _poll_and_route(self, sensor: Sensor) -> None:
        facts = await sensor.poll()
        if facts:
            await self.on_facts_sensed(facts, sensor.sensor_type)
        await self._snapshot_store.save(sensor.sensor_type, sensor.get_snapshot())

    def _resolve_route(self, fact: SensedFact) -> str:
        route = self._overrides.get(fact.source_type) or fact.cognition_route
        if route not in _VALID_ROUTES:
            logger.warning(
                "[SensorRegistry] Invalid cognition_route '%s' from '%s', fallback heartbeat",
                route,
                fact.source_type,
            )
            return "heartbeat"
        return route

    def _job_id(self, sensor_type: str) -> str:
        return f"sensor:{self._worker_id}:{sensor_type}"

    def _with_worker_id(
        self,
        payload: tuple[tuple[str, Any], ...],
    ) -> tuple[tuple[str, Any], ...]:
        if any(key == "worker_id" for key, _ in payload):
            return payload
        return payload + (("worker_id", self._worker_id),)
