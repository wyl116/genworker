# edition: baseline
from __future__ import annotations

import pytest

from src.memory.episodic.models import Episode, EpisodeSource
from src.memory.orchestrator import MemoryWriteEvent
from src.memory.provider import EpisodicMemoryProvider, SemanticMemoryProvider
from src.memory.write_models import (
    DecisionWritePayload,
    EpisodeWritePayload,
    PreferenceWritePayload,
    SemanticFactWritePayload,
)


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def index(
        self,
        *,
        scope: str,
        content: str,
        metadata: dict[str, object] | None = None,
        item_id: str | None = None,
        level: str = "L1",
    ) -> str:
        self.calls.append({
            "scope": scope,
            "content": content,
            "metadata": metadata or {},
            "item_id": item_id,
            "level": level,
        })
        return str(item_id or "mem-1")


def _episode() -> Episode:
    return Episode(
        episode_id="ep-1",
        created_at="2026-04-17T00:00:00+00:00",
        source=EpisodeSource(
            type="task_completion",
            skill_used="analysis-skill",
            trigger="task:task-1",
        ),
        summary="Recovered from timeout.",
        key_findings=("Retried downstream call",),
        related_entities=(),
        related_goals=("goal-1",),
        related_duties=("duty-1",),
    )


@pytest.mark.asyncio
async def test_semantic_provider_writes_semantic_fact() -> None:
    client = _RecordingClient()
    provider = SemanticMemoryProvider(client)

    await provider.write(MemoryWriteEvent(
        action="create",
        target="semantic_fact",
        entity_id="rule-1",
        content=SemanticFactWritePayload(
            tenant_id="demo",
            worker_id="worker-1",
            rule="先确认需求再动手",
            reason="减少返工",
        ),
        source_subsystem="rule",
        occurred_at="2026-04-17T00:00:00+00:00",
    ))

    assert len(client.calls) == 1
    assert client.calls[0]["scope"] == "viking://tenant/demo/worker/worker-1/memories/semantic"
    assert client.calls[0]["item_id"] == "rule-1"
    assert "Rule: 先确认需求再动手" in str(client.calls[0]["content"])
    assert client.calls[0]["metadata"]["target"] == "semantic_fact"


@pytest.mark.asyncio
async def test_semantic_provider_writes_preference_and_decision() -> None:
    client = _RecordingClient()
    provider = SemanticMemoryProvider(client)

    await provider.write(MemoryWriteEvent(
        action="create",
        target="preference",
        entity_id="pref-1",
        content=PreferenceWritePayload(
            tenant_id="demo",
            worker_id="worker-1",
            content="默认使用表格输出",
        ),
        source_subsystem="preference",
        occurred_at="2026-04-17T00:00:00+00:00",
    ))
    await provider.write(MemoryWriteEvent(
        action="create",
        target="decision",
        entity_id="dec-1",
        content=DecisionWritePayload(
            tenant_id="demo",
            worker_id="worker-1",
            decision="本周先修稳定性问题",
        ),
        source_subsystem="decision",
        occurred_at="2026-04-17T00:00:00+00:00",
    ))

    assert [call["item_id"] for call in client.calls] == ["pref-1", "dec-1"]
    assert client.calls[0]["metadata"]["preference"] == "默认使用表格输出"
    assert client.calls[1]["metadata"]["decision"] == "本周先修稳定性问题"


@pytest.mark.asyncio
async def test_episodic_provider_writes_episode_index() -> None:
    client = _RecordingClient()
    provider = EpisodicMemoryProvider(client)
    episode = _episode()

    await provider.write(MemoryWriteEvent(
        action="create",
        target="episode",
        entity_id=episode.episode_id,
        content=EpisodeWritePayload(
            tenant_id="demo",
            worker_id="worker-1",
            episode=episode,
        ),
        source_subsystem="episodic",
        occurred_at=episode.created_at,
    ))

    assert len(client.calls) == 1
    assert client.calls[0]["scope"] == "viking://tenant/demo/worker/worker-1/memories/episodic"
    assert client.calls[0]["item_id"] == "ep-1"
    assert client.calls[0]["metadata"]["goal_id"] == "goal-1"
    assert client.calls[0]["metadata"]["duty_id"] == "duty-1"
