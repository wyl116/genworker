"""Domain models for external source integrations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParsedGoalInfo:
    """LLM-extracted goal information candidate from external content."""

    title: str
    description: str
    milestones: tuple[dict[str, Any], ...]
    deadline: str | None = None
    priority: str = "medium"
    stakeholders: tuple[str, ...] = ()
    source_type: str = ""
    source_uri: str = ""
    raw_content: str = ""
    confidence: float = 0.0


@dataclass(frozen=True)
class SyncRecord:
    """Tracks each bidirectional sync operation."""

    sync_id: str
    goal_id: str
    direction: str
    channel: str
    synced_at: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class MonitorConfig:
    """External source monitoring configuration from PERSONA.md."""

    source_type: str
    poll_interval: str = "1h"
    filter: tuple[tuple[str, str], ...] = ()
    auto_create_goal: bool = False
    require_approval: bool = True
