"""Core sensing protocol and immutable fact model."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SensedFact:
    """One atomic fact discovered by a sensor."""

    source_type: str
    event_type: str
    dedupe_key: str
    payload: tuple[tuple[str, Any], ...] = ()
    priority_hint: int = 0
    cognition_route: str = "heartbeat"
    sensed_at: str = field(default_factory=_utc_now)

    @property
    def payload_dict(self) -> dict[str, Any]:
        return dict(self.payload)


FactCallback = Callable[[tuple[SensedFact, ...]], Awaitable[None]]


@runtime_checkable
class Sensor(Protocol):
    """Contract for pull or push based sensing sources."""

    @property
    def sensor_type(self) -> str:
        ...

    @property
    def delivery_mode(self) -> str:
        ...

    def set_fact_callback(self, callback: FactCallback) -> None:
        ...

    async def poll(self) -> tuple[SensedFact, ...]:
        ...

    async def start(self) -> None:
        ...

    async def stop(self) -> None:
        ...

    def get_snapshot(self) -> dict[str, Any]:
        ...

    def restore_snapshot(self, snapshot: dict[str, Any]) -> None:
        ...
