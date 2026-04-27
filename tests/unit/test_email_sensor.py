# edition: baseline
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.worker.sensing.config import RoutingRule
from src.worker.sensing.sensors.email_sensor import EmailSensor, _parse_email_results


class StubDeduplicator:
    def __init__(self, duplicates: set[str] | None = None) -> None:
        self.duplicates = duplicates or set()
        self.calls: list[tuple[str, str]] = []

    async def is_duplicate(self, channel_type: str, message_id: str) -> bool:
        self.calls.append((channel_type, message_id))
        return message_id in self.duplicates


@pytest.mark.asyncio
async def test_email_sensor_per_fact_routing() -> None:
    email_client = AsyncMock()
    email_client.search = AsyncMock(return_value=[
        {"message_id": "1", "subject": "周报", "from": "team@corp.com", "content": "..."},
        {"message_id": "2", "subject": "URGENT: DB down", "from": "alert@corp.com", "content": "..."},
        {"message_id": "3", "subject": "项目评审", "from": "boss@corp.com", "content": "..."},
    ])
    sensor = EmailSensor(
        email_client=email_client,
        filter_config={"subject_keywords": ""},
        routing_rules=(
            RoutingRule(field="subject", pattern="URGENT", match_mode="contains", route="reactive"),
            RoutingRule(field="from", pattern="boss@", match_mode="contains", route="both"),
        ),
        fallback_route="heartbeat",
    )

    facts = await sensor.poll()

    assert len(facts) == 3
    assert facts[0].cognition_route == "heartbeat"
    assert facts[1].cognition_route == "reactive"
    assert facts[2].cognition_route == "both"
    assert facts[1].priority_hint > facts[0].priority_hint


@pytest.mark.asyncio
async def test_email_sensor_restore_snapshot_skips_seen_messages() -> None:
    email_client = AsyncMock()
    email_client.search = AsyncMock(return_value=[
        {"message_id": "1", "subject": "Hello", "from": "a@corp.com", "content": "..."},
        {"message_id": "2", "subject": "Hello 2", "from": "b@corp.com", "content": "..."},
    ])
    sensor = EmailSensor(email_client=email_client, filter_config={})
    sensor.restore_snapshot({"seen_ids": ["1"]})

    facts = await sensor.poll()

    assert [fact.payload_dict["message_id"] for fact in facts] == ["2"]


@pytest.mark.asyncio
async def test_email_sensor_uses_shared_email_dedup_key() -> None:
    email_client = AsyncMock()
    email_client.search = AsyncMock(return_value=[
        {"message_id": "1", "subject": "URGENT", "from": "a@corp.com", "content": "..."},
    ])
    deduplicator = StubDeduplicator()
    sensor = EmailSensor(
        email_client=email_client,
        filter_config={},
        deduplicator=deduplicator,
    )

    await sensor.poll()

    assert deduplicator.calls == [("email", "1")]


def test_parse_email_results_accepts_json_string_payload() -> None:
    result = _parse_email_results('{"emails":[{"message_id":"1","subject":"Hello"}]}')

    assert result == [{"message_id": "1", "subject": "Hello"}]
