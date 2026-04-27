"""
Recent event recorder for operational introspection.

Stores a bounded in-memory timeline of recent EventBus traffic so ops APIs
can expose backend activity without parsing logs.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .models import Event


@dataclass(frozen=True)
class RecordedEvent:
    """Serializable event record with normalized timestamp and payload."""
    event_id: str
    type: str
    source: str
    tenant_id: str
    timestamp: str
    payload: dict[str, Any]
    metadata: dict[str, Any]
    worker_id: str = ""


class RecentEventRecorder:
    """Bounded in-memory store of recent events for ops visibility."""

    def __init__(self, max_events: int = 500) -> None:
        self._events: deque[RecordedEvent] = deque(maxlen=max_events)

    async def record(self, event: Event) -> None:
        """Record a single event from the EventBus."""
        payload = dict(event.payload)
        metadata = dict(event.metadata)
        worker_id = str(payload.get("worker_id", metadata.get("worker_id", "")))
        self._events.append(RecordedEvent(
            event_id=event.event_id,
            type=event.type,
            source=event.source,
            tenant_id=event.tenant_id,
            timestamp=event.timestamp or _now_iso(),
            payload=payload,
            metadata=metadata,
            worker_id=worker_id,
        ))

    def recent_events(
        self,
        tenant_id: str,
        worker_id: str = "",
        event_type: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return newest-first recent events matching the provided filters."""
        results: list[dict[str, Any]] = []
        for item in reversed(self._events):
            if item.tenant_id != tenant_id:
                continue
            if worker_id and item.worker_id != worker_id:
                continue
            if event_type and item.type != event_type:
                continue
            results.append(asdict(item))
            if len(results) >= limit:
                break
        return results


def _now_iso() -> str:
    """Current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()
