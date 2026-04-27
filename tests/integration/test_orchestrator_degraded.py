# edition: baseline
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.memory.episodic.models import Episode, EpisodeSource
from src.memory.episodic.store import write_episode_with_index
from src.memory.orchestrator import MemoryOrchestrator
from src.memory.provider import EpisodicMemoryProvider, SemanticMemoryProvider


class _TimeoutClient:
    async def search(self, **kwargs):
        raise asyncio.TimeoutError()


class _TimeoutIndexer:
    async def index_episode(self, episode):
        raise asyncio.TimeoutError()


def _episode() -> Episode:
    return Episode(
        episode_id="ep-timeout-1",
        created_at="2026-04-17T00:00:00+00:00",
        source=EpisodeSource(
            type="task_completion",
            skill_used="analysis-skill",
            trigger="task:task-1",
        ),
        summary="Recovered from downstream timeout.",
        key_findings=(),
        related_entities=(),
    )


@pytest.mark.asyncio
async def test_orchestrator_fails_open_when_viking_down(tmp_path: Path) -> None:
    orchestrator = MemoryOrchestrator((
        SemanticMemoryProvider(_TimeoutClient()),
        EpisodicMemoryProvider(_TimeoutClient()),
    ))

    result = await orchestrator.query(
        "test query",
        worker_id="worker-1",
        tenant_id="demo",
    )

    assert result.merged_context == ""
    assert result.semantic_results == ()
    assert result.episodic_results == ()

    memory_dir = tmp_path / "memory"
    md_path = await write_episode_with_index(
        memory_dir,
        _episode(),
        viking_indexer=_TimeoutIndexer(),
    )

    assert md_path == memory_dir / "episodes" / "ep-timeout-1.md"
    assert md_path.exists()

