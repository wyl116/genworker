"""Passive push sensor fed by the HTTP API layer."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from ..base import SensorBase
from ..protocol import SensedFact


class WebhookSensor(SensorBase):
    """Convert webhook payloads into sensed facts."""

    @property
    def sensor_type(self) -> str:
        return "webhook"

    @property
    def delivery_mode(self) -> str:
        return "push"

    async def ingest(self, raw: dict[str, Any]) -> None:
        data = raw.get("data", {})
        payload = (
            tuple((str(key), value) for key, value in data.items())
            if isinstance(data, dict) else ()
        )
        route = str(raw.get("cognition_route", "")).strip() or self._classify_route(payload)
        fact = SensedFact(
            source_type="webhook",
            event_type=str(raw.get("event_type", "external.webhook")),
            dedupe_key=str(raw.get("dedupe_key", f"webhook:{uuid4().hex[:8]}")),
            payload=payload,
            priority_hint=int(raw.get("priority", 20)),
            cognition_route=route,
        )
        if self._fact_callback is not None:
            await self._fact_callback((fact,))

    async def start(self) -> None:
        assert self._fact_callback is not None, "set_fact_callback() must be called before start()"
