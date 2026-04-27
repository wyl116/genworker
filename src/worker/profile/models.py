"""Worker behavior profile models."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillUsageEntry:
    skill_id: str
    count: int
    ratio: float


@dataclass(frozen=True)
class RuleApplicationEntry:
    rule_id: str
    apply_count: int
    confidence: float
    success_correlation: float = 0.0


@dataclass(frozen=True)
class TaskTypeDistribution:
    writing: int = 0
    coding: int = 0
    analysis: int = 0
    operations: int = 0
    other: int = 0


@dataclass(frozen=True)
class BehavioralTrend:
    trend_type: str
    description: str
    confidence: float
    detected_at: str


@dataclass(frozen=True)
class WorkerBehaviorProfile:
    worker_id: str
    updated_at: str
    task_count_total: int
    task_count_last_30d: int
    skill_usage: tuple[SkillUsageEntry, ...]
    rule_applications: tuple[RuleApplicationEntry, ...]
    task_type_distribution: TaskTypeDistribution
    behavioral_trends: tuple[BehavioralTrend, ...]
    avg_task_duration_seconds: float
    success_rate: float
