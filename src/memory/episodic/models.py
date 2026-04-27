"""Data models for the episodic memory system.

All models use frozen dataclasses with tuple/frozenset for collections,
ensuring immutability throughout the memory pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RelatedEntity:
    """An entity referenced by an episode (region, metric, system, user, etc.)."""

    type: str   # "region" | "metric" | "system" | "user" | ...
    value: str


@dataclass(frozen=True)
class EpisodeSource:
    """Origin metadata describing how an episode was created."""

    type: str         # "task_completion" | "duty_execution" | "goal_progress"
    skill_used: str
    trigger: str | None = None  # e.g. "duty:daily-quality-check"


@dataclass(frozen=True)
class Episode:
    """A single episodic memory entry with full metadata."""

    episode_id: str
    created_at: str          # ISO 8601
    source: EpisodeSource
    summary: str
    key_findings: tuple[str, ...]
    related_entities: tuple[RelatedEntity, ...]
    related_goals: tuple[str, ...] = ()
    related_duties: tuple[str, ...] = ()
    relevance_score: float = 0.9
    last_retrieved: str | None = None
    retrieve_count: int = 0


@dataclass(frozen=True)
class EpisodeIndex:
    """Flattened index entry derived from episode markdown for recall/ranking."""

    id: str
    ts: str
    summary: str
    entities: tuple[str, ...]   # flattened entity values
    skills: tuple[str, ...]
    duties: tuple[str, ...]
    goals: tuple[str, ...]
    score: float


@dataclass(frozen=True)
class EpisodeQuery:
    """A retrieval request specifying search criteria."""

    keywords: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    skill_id: str | None = None
    duty_id: str | None = None
    goal_id: str | None = None
    top_k: int = 5
