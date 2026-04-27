# edition: baseline
from __future__ import annotations

import pytest

from src.events.bus import EventBus
from src.events.models import Subscription
from src.memory.orchestrator import MemoryOrchestrator, MemoryWriteEvent
from src.memory.provider import MemoryProvider, MemoryRecallResult
from src.memory.write_models import PreferenceWritePayload


class _Provider(MemoryProvider):
    name = "semantic"

    async def query(self, text: str, worker_id: str, **kwargs):
        return MemoryRecallResult(source=self.name)


@pytest.mark.asyncio
async def test_on_memory_write_publishes_event():
    event_bus = EventBus()
    captured = []

    async def _handler(event):
        captured.append(event)

    event_bus.subscribe(Subscription(
        handler_id="memory-event-test",
        event_type="memory.written",
        tenant_id="demo",
        handler=_handler,
    ))
    orchestrator = MemoryOrchestrator(
        providers=(_Provider(),),
        event_bus=event_bus,
    )

    await orchestrator.on_memory_write(MemoryWriteEvent(
        action="create",
        target="preference",
        entity_id="pref-1",
        content=PreferenceWritePayload(
            tenant_id="demo",
            worker_id="w1",
            content="表格格式",
        ),
        source_subsystem="episodic",
        occurred_at="2026-04-11T00:00:00+00:00",
    ))

    assert len(captured) == 1
    payload = dict(captured[0].payload)
    assert payload["target"] == "preference"
    assert payload["worker_id"] == "w1"
