# edition: baseline
"""Tests for episodic memory decay and archive candidate identification."""

from pathlib import Path

import pytest

from src.memory.episodic.decay import (
    DEFAULT_ARCHIVE_THRESHOLD,
    DEFAULT_DECAY_FACTOR,
    RETRIEVAL_BOOST,
    compute_decayed_score,
    identify_archive_candidates,
    run_decay_cycle,
)
from src.memory.episodic.models import (
    Episode,
    EpisodeIndex,
    EpisodeSource,
    RelatedEntity,
)
from src.memory.episodic.store import write_episode, load_index


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_index(
    id: str = "ep-001",
    ts: str = "2026-03-01T10:00:00",
    score: float = 0.9,
) -> EpisodeIndex:
    return EpisodeIndex(
        id=id,
        ts=ts,
        summary="Test summary",
        entities=("us-east-1",),
        skills=("data-analysis",),
        duties=("duty-daily-check",),
        goals=("goal-alpha",),
        score=score,
    )


def _make_episode(
    episode_id: str = "ep-001",
    created_at: str = "2026-03-01T10:00:00",
    relevance_score: float = 0.9,
) -> Episode:
    return Episode(
        episode_id=episode_id,
        created_at=created_at,
        source=EpisodeSource(
            type="task_completion",
            skill_used="data-analysis",
        ),
        summary="Test summary",
        key_findings=("finding one",),
        related_entities=(RelatedEntity(type="region", value="us-east-1"),),
        related_goals=("goal-alpha",),
        related_duties=("duty-daily-check",),
        relevance_score=relevance_score,
    )


@pytest.fixture
def base_dir(tmp_path: Path) -> Path:
    return tmp_path


# ---------------------------------------------------------------------------
# compute_decayed_score
# ---------------------------------------------------------------------------


class TestComputeDecayedScore:
    def test_no_decay_at_day_zero(self) -> None:
        score = compute_decayed_score(
            initial_score=0.9,
            days_since_creation=0,
            retrieve_count=0,
        )
        assert score == pytest.approx(0.9, abs=0.01)

    def test_30_day_decay(self) -> None:
        """After 30 days, score should be approximately 0.55 * initial."""
        score = compute_decayed_score(
            initial_score=1.0,
            days_since_creation=30,
            retrieve_count=0,
        )
        # 0.98^30 ~= 0.5455
        assert score == pytest.approx(0.55, abs=0.02)

    def test_90_day_decay(self) -> None:
        """After 90 days, score should be approximately 0.16 * initial."""
        score = compute_decayed_score(
            initial_score=1.0,
            days_since_creation=90,
            retrieve_count=0,
        )
        # 0.98^90 ~= 0.1626
        assert score == pytest.approx(0.16, abs=0.02)

    def test_retrieval_boost(self) -> None:
        """One retrieval should boost score by RETRIEVAL_BOOST (0.1)."""
        score_no_retrieval = compute_decayed_score(
            initial_score=0.9,
            days_since_creation=30,
            retrieve_count=0,
        )
        score_one_retrieval = compute_decayed_score(
            initial_score=0.9,
            days_since_creation=30,
            retrieve_count=1,
        )
        diff = score_one_retrieval - score_no_retrieval
        assert diff == pytest.approx(RETRIEVAL_BOOST, abs=0.01)

    def test_retrieval_boost_capped_at_initial(self) -> None:
        """Boost should not exceed initial_score."""
        score = compute_decayed_score(
            initial_score=0.5,
            days_since_creation=0,
            retrieve_count=10,  # 10 * 0.1 = 1.0 > 0.5 initial
        )
        assert score <= 0.5

    def test_important_flag_slows_decay(self) -> None:
        """Marked important episodes decay more slowly."""
        normal = compute_decayed_score(
            initial_score=0.9,
            days_since_creation=60,
            retrieve_count=0,
        )
        important = compute_decayed_score(
            initial_score=0.9,
            days_since_creation=60,
            retrieve_count=0,
            is_marked_important=True,
        )
        assert important > normal

    def test_negative_days_treated_as_zero(self) -> None:
        score = compute_decayed_score(
            initial_score=0.9,
            days_since_creation=-5,
            retrieve_count=0,
        )
        assert score == pytest.approx(0.9, abs=0.01)

    def test_result_never_negative(self) -> None:
        score = compute_decayed_score(
            initial_score=0.1,
            days_since_creation=10000,
            retrieve_count=0,
        )
        assert score >= 0.0

    def test_custom_decay_factor(self) -> None:
        """A lower decay factor causes faster decay."""
        fast = compute_decayed_score(
            initial_score=0.9,
            days_since_creation=30,
            retrieve_count=0,
            decay_factor=0.95,
        )
        slow = compute_decayed_score(
            initial_score=0.9,
            days_since_creation=30,
            retrieve_count=0,
            decay_factor=0.99,
        )
        assert fast < slow


# ---------------------------------------------------------------------------
# identify_archive_candidates
# ---------------------------------------------------------------------------


class TestIdentifyArchiveCandidates:
    def test_below_threshold_is_candidate(self) -> None:
        # 200 days old with score 0.9 -> 0.98^200 * 0.9 ~= 0.016 < 0.05
        idx = _make_index(ts="2025-08-01T10:00:00", score=0.9)
        candidates = identify_archive_candidates(
            (idx,),
            current_date="2026-04-01T10:00:00",
            archive_threshold=DEFAULT_ARCHIVE_THRESHOLD,
        )
        assert "ep-001" in candidates

    def test_above_threshold_not_candidate(self) -> None:
        # 5 days old -> 0.98^5 * 0.9 ~= 0.81, well above threshold
        idx = _make_index(ts="2026-03-27T10:00:00", score=0.9)
        candidates = identify_archive_candidates(
            (idx,),
            current_date="2026-04-01T10:00:00",
        )
        assert "ep-001" not in candidates

    def test_max_active_eviction(self) -> None:
        """When exceeding max_active, lowest-scoring episodes are evicted."""
        indices = tuple(
            _make_index(
                id=f"ep-{i:03d}",
                ts="2026-03-31T10:00:00",
                score=0.1 * (i + 1),
            )
            for i in range(10)
        )
        # max_active=7, so 3 lowest should be evicted
        candidates = identify_archive_candidates(
            indices,
            current_date="2026-04-01T10:00:00",
            max_active=7,
            archive_threshold=0.0,  # disable threshold-based archiving
        )
        assert len(candidates) == 3
        # The 3 lowest-scoring ones
        assert "ep-000" in candidates
        assert "ep-001" in candidates
        assert "ep-002" in candidates

    def test_empty_indices(self) -> None:
        candidates = identify_archive_candidates(
            (),
            current_date="2026-04-01T10:00:00",
        )
        assert candidates == ()

    def test_combined_threshold_and_capacity(self) -> None:
        """Both threshold and capacity eviction can apply simultaneously."""
        # One very old (below threshold), rest recent
        old_idx = _make_index(id="ep-old", ts="2025-01-01T10:00:00", score=0.5)
        recent = tuple(
            _make_index(
                id=f"ep-{i:03d}",
                ts="2026-03-30T10:00:00",
                score=0.5 + 0.05 * i,
            )
            for i in range(6)
        )
        all_indices = (old_idx,) + recent

        candidates = identify_archive_candidates(
            all_indices,
            current_date="2026-04-01T10:00:00",
            max_active=4,
        )
        # old_idx should be archived by threshold
        assert "ep-old" in candidates


# ---------------------------------------------------------------------------
# run_decay_cycle
# ---------------------------------------------------------------------------


class TestRunDecayCycle:
    @pytest.mark.asyncio
    async def test_basic_cycle(self, base_dir: Path) -> None:
        """Run decay cycle updates scores and removes archived episodes from index."""
        ep = _make_episode(
            episode_id="ep-recent",
            created_at="2026-03-30T10:00:00",
            relevance_score=0.9,
        )
        write_episode(base_dir, ep)

        updated, archived = await run_decay_cycle(
            memory_dir=base_dir,
            current_date="2026-04-01T10:00:00",
        )
        assert updated == 1
        assert archived == 0

        # Index should still have the entry
        indices = load_index(base_dir)
        assert len(indices) == 1

    @pytest.mark.asyncio
    async def test_cycle_archives_old_entries(self, base_dir: Path) -> None:
        """Very old episodes should be archived and removed from index."""
        ep = _make_episode(
            episode_id="ep-ancient",
            created_at="2024-01-01T10:00:00",
            relevance_score=0.5,
        )
        write_episode(base_dir, ep)

        updated, archived = await run_decay_cycle(
            memory_dir=base_dir,
            current_date="2026-04-01T10:00:00",
        )
        assert archived == 1

        # Index should be empty after archiving
        indices = load_index(base_dir)
        assert len(indices) == 0

    @pytest.mark.asyncio
    async def test_cycle_empty_dir(self, base_dir: Path) -> None:
        updated, archived = await run_decay_cycle(
            memory_dir=base_dir,
            current_date="2026-04-01T10:00:00",
        )
        assert updated == 0
        assert archived == 0

    @pytest.mark.asyncio
    async def test_cycle_with_max_active(self, base_dir: Path) -> None:
        """Capacity eviction during decay cycle."""
        for i in range(5):
            ep = _make_episode(
                episode_id=f"ep-{i:03d}",
                created_at="2026-03-30T10:00:00",
                relevance_score=0.5 + 0.1 * i,
            )
            write_episode(base_dir, ep)

        updated, archived = await run_decay_cycle(
            memory_dir=base_dir,
            current_date="2026-04-01T10:00:00",
            max_active=3,
        )
        assert updated == 5
        assert archived == 2

        indices = load_index(base_dir)
        assert len(indices) == 3
