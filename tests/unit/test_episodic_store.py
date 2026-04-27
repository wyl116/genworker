# edition: baseline
"""Tests for episodic memory store: Markdown source and derived indexing."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.memory.episodic.models import (
    Episode,
    EpisodeIndex,
    EpisodeSource,
    RelatedEntity,
)
from src.memory.episodic.store import (
    IndexFileLock,
    episode_to_index,
    episode_to_markdown,
    load_episode,
    load_index,
    markdown_to_episode,
    rebuild_index,
    write_episode,
    write_episode_with_index,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_episode(
    episode_id: str = "ep-001",
    summary: str = "Test episode summary",
    skill_used: str = "data-analysis",
    created_at: str = "2026-03-01T10:00:00",
    relevance_score: float = 0.9,
) -> Episode:
    """Factory for creating test Episode instances."""
    return Episode(
        episode_id=episode_id,
        created_at=created_at,
        source=EpisodeSource(
            type="task_completion",
            skill_used=skill_used,
            trigger="duty:daily-quality-check",
        ),
        summary=summary,
        key_findings=(
            "Finding one about data quality",
            "Finding two about latency",
        ),
        related_entities=(
            RelatedEntity(type="region", value="us-east-1"),
            RelatedEntity(type="metric", value="p99_latency"),
        ),
        related_goals=("goal-alpha",),
        related_duties=("duty-daily-check",),
        relevance_score=relevance_score,
    )


@pytest.fixture
def sample_episode() -> Episode:
    return _make_episode()


@pytest.fixture
def base_dir(tmp_path: Path) -> Path:
    return tmp_path


# ---------------------------------------------------------------------------
# episode_to_index
# ---------------------------------------------------------------------------


class TestEpisodeToIndex:
    def test_basic_conversion(self, sample_episode: Episode) -> None:
        idx = episode_to_index(sample_episode)
        assert idx.id == "ep-001"
        assert idx.ts == "2026-03-01T10:00:00"
        assert idx.summary == "Test episode summary"
        assert "us-east-1" in idx.entities
        assert "p99_latency" in idx.entities
        assert idx.skills == ("data-analysis",)
        assert idx.duties == ("duty-daily-check",)
        assert idx.goals == ("goal-alpha",)
        assert idx.score == 0.9

    def test_entities_are_flattened_values(self, sample_episode: Episode) -> None:
        idx = episode_to_index(sample_episode)
        # Only values, not types
        assert "region" not in idx.entities
        assert "us-east-1" in idx.entities


# ---------------------------------------------------------------------------
# episode_to_markdown / markdown_to_episode round-trip
# ---------------------------------------------------------------------------


class TestMarkdownSerialization:
    def test_round_trip(self, sample_episode: Episode) -> None:
        md = episode_to_markdown(sample_episode)
        restored = markdown_to_episode(md)

        assert restored.episode_id == sample_episode.episode_id
        assert restored.created_at == sample_episode.created_at
        assert restored.source.type == sample_episode.source.type
        assert restored.source.skill_used == sample_episode.source.skill_used
        assert restored.source.trigger == sample_episode.source.trigger
        assert restored.summary == sample_episode.summary
        assert restored.key_findings == sample_episode.key_findings
        assert restored.related_entities == sample_episode.related_entities
        assert restored.related_goals == sample_episode.related_goals
        assert restored.related_duties == sample_episode.related_duties
        assert restored.relevance_score == sample_episode.relevance_score

    def test_markdown_contains_summary_heading(self, sample_episode: Episode) -> None:
        md = episode_to_markdown(sample_episode)
        assert "# Test episode summary" in md

    def test_markdown_contains_key_findings(self, sample_episode: Episode) -> None:
        md = episode_to_markdown(sample_episode)
        assert "## Key Findings" in md
        assert "- Finding one about data quality" in md
        assert "- Finding two about latency" in md

    def test_empty_key_findings(self) -> None:
        ep = Episode(
            episode_id="ep-empty",
            created_at="2026-03-01T10:00:00",
            source=EpisodeSource(type="task_completion", skill_used="s1"),
            summary="No findings",
            key_findings=(),
            related_entities=(),
        )
        md = episode_to_markdown(ep)
        restored = markdown_to_episode(md)
        assert restored.key_findings == ()
        assert restored.summary == "No findings"


# ---------------------------------------------------------------------------
# write_episode / load_episode
# ---------------------------------------------------------------------------


class TestWriteAndLoadEpisode:
    def test_write_creates_md_file(
        self, base_dir: Path, sample_episode: Episode
    ) -> None:
        result_path = write_episode(base_dir, sample_episode)
        assert result_path.exists()
        assert result_path.suffix == ".md"
        assert result_path.name == "ep-001.md"

    def test_write_no_longer_creates_jsonl_index(
        self, base_dir: Path, sample_episode: Episode
    ) -> None:
        write_episode(base_dir, sample_episode)
        assert not (base_dir / "index.jsonl").exists()

    def test_load_index_scans_markdown_sources(self, base_dir: Path) -> None:
        ep1 = _make_episode(episode_id="ep-001")
        ep2 = _make_episode(episode_id="ep-002", summary="Second episode")
        write_episode(base_dir, ep1)
        write_episode(base_dir, ep2)

        indices = load_index(base_dir)
        assert len(indices) == 2
        assert {item.id for item in indices} == {"ep-001", "ep-002"}

    def test_load_episode_round_trip(
        self, base_dir: Path, sample_episode: Episode
    ) -> None:
        write_episode(base_dir, sample_episode)
        loaded = load_episode(base_dir, "ep-001")
        assert loaded.episode_id == sample_episode.episode_id
        assert loaded.summary == sample_episode.summary
        assert loaded.key_findings == sample_episode.key_findings
        assert loaded.related_entities == sample_episode.related_entities

    def test_load_episode_not_found(self, base_dir: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_episode(base_dir, "nonexistent")


# ---------------------------------------------------------------------------
# load_index
# ---------------------------------------------------------------------------


class TestLoadIndex:
    def test_empty_when_no_file(self, base_dir: Path) -> None:
        result = load_index(base_dir)
        assert result == ()

    def test_loads_written_entries(self, base_dir: Path) -> None:
        ep1 = _make_episode(episode_id="ep-001")
        ep2 = _make_episode(episode_id="ep-002")
        write_episode(base_dir, ep1)
        write_episode(base_dir, ep2)

        indices = load_index(base_dir)
        assert len(indices) == 2
        ids = {idx.id for idx in indices}
        assert ids == {"ep-001", "ep-002"}

    def test_skips_malformed_markdown(self, base_dir: Path) -> None:
        episodes_dir = base_dir / "episodes"
        episodes_dir.mkdir(parents=True, exist_ok=True)
        (episodes_dir / "broken.md").write_text("not valid", encoding="utf-8")
        result = load_index(base_dir)
        assert result == ()


# ---------------------------------------------------------------------------
# rebuild_index
# ---------------------------------------------------------------------------


class TestRebuildIndex:
    def test_rebuild_from_episodes(self, base_dir: Path) -> None:
        ep1 = _make_episode(episode_id="ep-001", created_at="2026-03-01T10:00:00")
        ep2 = _make_episode(episode_id="ep-002", created_at="2026-03-02T10:00:00")
        write_episode(base_dir, ep1)
        write_episode(base_dir, ep2)

        indices = rebuild_index(base_dir)
        assert len(indices) == 2
        assert indices[0].id == "ep-001"  # sorted by ts
        assert indices[1].id == "ep-002"

    def test_rebuild_empty_dir(self, base_dir: Path) -> None:
        indices = rebuild_index(base_dir)
        assert indices == ()

    def test_rebuild_returns_scanned_entries(self, base_dir: Path) -> None:
        ep = _make_episode(episode_id="ep-001")
        write_episode(base_dir, ep)

        rebuild_index(base_dir)
        indices = load_index(base_dir)
        assert len(indices) == 1
        assert indices[0].id == "ep-001"


class TestWriteEpisodeWithIndex:
    @pytest.mark.asyncio
    async def test_write_episode_with_index_calls_indexer(self, base_dir: Path) -> None:
        episode = _make_episode()
        indexer = AsyncMock()

        result = await write_episode_with_index(
            base_dir,
            episode,
            viking_indexer=indexer,
        )

        assert result.exists()
        indexer.index_episode.assert_awaited_once_with(episode)

    @pytest.mark.asyncio
    async def test_write_episode_with_index_swallows_index_errors(self, base_dir: Path) -> None:
        episode = _make_episode()
        indexer = AsyncMock()
        indexer.index_episode.side_effect = RuntimeError("boom")

        result = await write_episode_with_index(
            base_dir,
            episode,
            viking_indexer=indexer,
        )

        assert result.exists()
        assert load_episode(base_dir, episode.episode_id).summary == episode.summary


# ---------------------------------------------------------------------------
# IndexFileLock
# ---------------------------------------------------------------------------


class TestIndexFileLock:
    @pytest.mark.asyncio
    async def test_lock_basic_usage(self) -> None:
        """Lock can be used as async context manager."""
        lock = IndexFileLock()
        async with lock:
            pass  # no error

    @pytest.mark.asyncio
    async def test_concurrent_writes_produce_all_markdown_files(self, base_dir: Path) -> None:
        lock = IndexFileLock()
        episodes = [
            _make_episode(episode_id=f"ep-{i:03d}", summary=f"Episode {i}")
            for i in range(20)
        ]

        async def _write(ep: Episode) -> None:
            async with lock:
                write_episode(base_dir, ep)

        await asyncio.gather(*[_write(ep) for ep in episodes])

        ids = {item.id for item in load_index(base_dir)}
        assert len(ids) == 20
