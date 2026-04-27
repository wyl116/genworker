"""
SubAgent data models - frozen dataclasses for parallel sub-goal execution.

Defines:
- SubAgentContext: creation context with sandboxed resources
- SubAgentHandle: external handle for tracking and control
- SubAgentUsage: resource usage statistics
- SubAgentResult: execution result
- AggregatedResult: merged results from multiple SubAgents
- SubAgentEvent: lifecycle event for EventBus reporting
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.memory.episodic.models import EpisodeIndex
from src.worker.planning.models import SubGoal
from src.worker.rules.models import Rule


@dataclass(frozen=True)
class SubAgentContext:
    """SubAgent creation context, populated by the host Worker."""
    agent_id: str                                   # e.g. "sa-{parent_worker_id}-{seq}"
    parent_worker_id: str
    parent_task_id: str
    sub_goal: SubGoal
    skill_id: str | None = None
    preferred_skill_ids: tuple[str, ...] = ()
    delegate_worker_id: str | None = None
    tool_sandbox: tuple[str, ...] = ()              # allowed tool IDs (subset of host)
    memory_snapshot: tuple[EpisodeIndex, ...] = ()  # read-only memory snapshot
    rules_snapshot: tuple[Rule, ...] = ()            # read-only rules snapshot
    pre_script: Any | None = None
    max_rounds: int = 10
    timeout_seconds: int = 120
    mode: str = "async"                             # "sync" | "async"


@dataclass(frozen=True)
class SubAgentHandle:
    """External handle for tracking and controlling a SubAgent."""
    agent_id: str
    sub_goal_id: str
    status: str = "pending"           # "pending" | "running" | "completed" | "failed" | "cancelled"
    created_at: str = ""              # ISO 8601


@dataclass(frozen=True)
class SubAgentUsage:
    """Resource usage statistics for a SubAgent execution."""
    total_tokens: int = 0
    tool_calls: int = 0
    duration_ms: int = 0


@dataclass(frozen=True)
class SubAgentResult:
    """Result from a single SubAgent execution."""
    agent_id: str
    sub_goal_id: str
    status: str                       # "success" | "failure" | "timeout" | "cancelled"
    content: str
    structured_data: tuple[tuple[str, Any], ...] = ()
    error: str | None = None
    usage: SubAgentUsage | None = None


@dataclass(frozen=True)
class AggregatedResult:
    """Merged results from multiple SubAgents."""
    sub_results: tuple[SubAgentResult, ...]
    success_count: int
    failure_count: int
    combined_content: str


@dataclass(frozen=True)
class SubAgentEvent:
    """Lifecycle event published via EventBus."""
    event_type: str                   # "started" | "progress" | "completed" | "failed"
    agent_id: str
    parent_task_id: str
    detail: str = ""
