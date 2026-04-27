"""Episodic memory decay and archive candidate identification.

Provides pure functions for time-based score decay and archive triggering.
The decay model: score(t) = initial * decay^days + retrieval_boost.
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from src.memory.episodic.models import EpisodeIndex

logger = logging.getLogger(__name__)

DEFAULT_DECAY_FACTOR = 0.98
DEFAULT_ARCHIVE_THRESHOLD = 0.05
RETRIEVAL_BOOST = 0.1


def compute_decayed_score(
    initial_score: float,
    days_since_creation: int,
    retrieve_count: int,
    decay_factor: float = DEFAULT_DECAY_FACTOR,
    is_marked_important: bool = False,
) -> float:
    """Pure function: compute time-decayed relevance score.

    Formula: score(t) = initial * effective_decay^days + retrieval_boost
    - retrieval_boost = min(retrieve_count * RETRIEVAL_BOOST, initial_score)
    - When is_marked_important, effective decay = sqrt(decay_factor) (slower decay)
    - Result is clamped to [0.0, initial_score]

    Args:
        initial_score: The original relevance score (0.0-1.0).
        days_since_creation: Number of days since the episode was created.
        retrieve_count: How many times the episode has been retrieved.
        decay_factor: Base decay factor per day (default 0.98).
        is_marked_important: If True, decay rate is halved.

    Returns:
        The decayed score, clamped between 0.0 and initial_score.
    """
    if days_since_creation < 0:
        days_since_creation = 0

    effective_decay = math.sqrt(decay_factor) if is_marked_important else decay_factor
    base_score = initial_score * (effective_decay ** days_since_creation)
    boost = min(retrieve_count * RETRIEVAL_BOOST, initial_score)
    result = base_score + boost
    return min(max(result, 0.0), initial_score)


def identify_archive_candidates(
    indices: tuple[EpisodeIndex, ...],
    current_date: str,
    archive_threshold: float = DEFAULT_ARCHIVE_THRESHOLD,
    max_active: int = 500,
) -> tuple[str, ...]:
    """Pure function: identify episodes that should be archived.

    Archive conditions (either triggers archival):
    1. score < archive_threshold
    2. Active count exceeds max_active -- evict lowest-scoring episodes
       (ties broken by preferring episodes with retrieve_count=0, approximated
       by lower score since we don't have retrieve_count in the index)

    Args:
        indices: All active episode index entries.
        current_date: Current date as ISO 8601 string for age calculation.
        archive_threshold: Score below which episodes are archived.
        max_active: Maximum number of active episodes before eviction.

    Returns:
        Tuple of episode IDs that are archive candidates.
    """
    if not indices:
        return ()

    current_dt = _parse_date(current_date)

    scored_entries: list[tuple[str, float]] = []
    below_threshold: list[str] = []

    for idx in indices:
        created_dt = _parse_date(idx.ts)
        days = max((current_dt - created_dt).days, 0)
        decayed = compute_decayed_score(
            initial_score=idx.score,
            days_since_creation=days,
            retrieve_count=0,
        )
        if decayed < archive_threshold:
            below_threshold.append(idx.id)
        scored_entries.append((idx.id, decayed))

    # Capacity eviction: if still too many active after removing threshold candidates
    threshold_ids = frozenset(below_threshold)
    remaining = [
        (eid, score) for eid, score in scored_entries if eid not in threshold_ids
    ]

    evicted: list[str] = []
    if len(remaining) > max_active:
        remaining_sorted = sorted(remaining, key=lambda x: x[1])
        evict_count = len(remaining) - max_active
        evicted = [eid for eid, _ in remaining_sorted[:evict_count]]

    all_candidates = frozenset(below_threshold) | frozenset(evicted)
    return tuple(all_candidates)


def _parse_date(date_str: str) -> datetime:
    """Parse an ISO 8601 date string to datetime, handling common formats."""
    clean = date_str.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(clean, fmt)
        except ValueError:
            continue
    # Fallback: strip timezone info and try again
    if "+" in clean:
        clean = clean[: clean.index("+")]
    if clean.endswith("Z"):
        clean = clean[:-1]
    return datetime.fromisoformat(clean)


async def run_decay_cycle(
    memory_dir: Path,
    current_date: str,
    decay_factor: float = DEFAULT_DECAY_FACTOR,
    archive_threshold: float = DEFAULT_ARCHIVE_THRESHOLD,
    max_active: int = 500,
    archive_manager: object | None = None,
    viking_indexer: object | None = None,
) -> tuple[int, int]:
    """Load episodes, recompute scores, and archive low-value entries.

    Args:
        memory_dir: Base directory for episodic memory storage.
        current_date: Current date as ISO 8601 string.
        decay_factor: Decay factor per day.
        archive_threshold: Score threshold for archival.
        max_active: Maximum active episodes before eviction.
        archive_manager: Optional archive manager (future integration).

    Returns:
        Tuple of (updated_count, archived_count).
    """
    from src.memory.episodic.store import load_episode, load_index, write_episode

    indices = load_index(memory_dir)
    if not indices:
        return (0, 0)

    current_dt = _parse_date(current_date)

    # Recompute scores for all indices
    updated_indices: list[EpisodeIndex] = []
    for idx in indices:
        created_dt = _parse_date(idx.ts)
        days = max((current_dt - created_dt).days, 0)
        new_score = compute_decayed_score(
            initial_score=idx.score,
            days_since_creation=days,
            retrieve_count=0,
        )
        updated_indices.append(replace(idx, score=new_score))

    updated_tuple = tuple(updated_indices)

    # Identify archive candidates
    candidates = identify_archive_candidates(
        updated_tuple,
        current_date,
        archive_threshold=archive_threshold,
        max_active=max_active,
    )
    candidate_set = frozenset(candidates)

    updated_count = len(indices)
    archived_count = len(candidates)

    for idx in updated_tuple:
        episode = load_episode(memory_dir, idx.id)
        if idx.id in candidate_set:
            episode_path = memory_dir / "episodes" / f"{idx.id}.md"
            if archive_manager is not None and episode_path.exists():
                from src.worker.archive.archive_manager import ArchiveMetadata

                await archive_manager.archive_episode(
                    episode_path,
                    ArchiveMetadata(
                        archived_at=current_dt.isoformat(),
                        archived_by="system",
                        reason="episodic_decay",
                    ),
                )
            elif episode_path.exists():
                episode_path.unlink()
            if viking_indexer is not None:
                try:
                    await viking_indexer.delete_episode(idx.id)
                except Exception:
                    pass
            continue

        refreshed = replace(episode, relevance_score=idx.score)
        write_episode(memory_dir, refreshed)
        if viking_indexer is not None:
            try:
                await viking_indexer.update_episode_score(idx.id, idx.score)
            except Exception:
                try:
                    await viking_indexer.index_episode(refreshed)
                except Exception:
                    pass

    logger.info(
        "Decay cycle: %d updated, %d archived", updated_count, archived_count
    )
    return (updated_count, archived_count)
