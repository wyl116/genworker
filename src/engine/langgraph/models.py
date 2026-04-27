"""Runtime models for the LangGraph engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from src.engine.state import UsageBudget
from src.skills.models import NodeDefinition, Skill


class LangGraphInitError(RuntimeError):
    """Raised when a langgraph skill cannot be initialized."""


class StateDriftError(RuntimeError):
    """Raised when resume state no longer matches the inbox digest."""


class BudgetExceededError(RuntimeError):
    """Raised when node-level execution exceeds the configured budget."""


@dataclass
class BudgetTracker:
    """Mutable budget tracker used inside graph node closures."""

    max_tokens: int = 0
    used_tokens: int = 0

    @classmethod
    def from_budget(cls, budget: UsageBudget | None) -> "BudgetTracker":
        if budget is None:
            return cls()
        return cls(max_tokens=budget.max_tokens, used_tokens=budget.used_tokens)

    def snapshot(self) -> UsageBudget:
        return UsageBudget(
            max_tokens=self.max_tokens,
            used_tokens=self.used_tokens,
        )

    @property
    def exceeded(self) -> bool:
        if self.max_tokens <= 0:
            return False
        return self.used_tokens >= self.max_tokens

    def add_usage(self, tokens: int) -> None:
        self.used_tokens += max(int(tokens or 0), 0)


@dataclass(frozen=True)
class CompiledGraphBundle:
    """Compiled graph and metadata needed for execution and resume."""

    skill: Skill
    compiled: Any
    state_whitelist: tuple[str, ...]
    interrupt_nodes: Mapping[str, NodeDefinition] = field(default_factory=dict)
    max_steps: int = 50


@dataclass(frozen=True)
class LangGraphCheckpointRecord:
    """Latest persisted checkpoint metadata for one thread."""

    tenant_id: str
    worker_id: str
    skill_id: str
    thread_id: str
    checkpoint_id: str
    round_number: int
    created_at: str
    lg_checkpoint: Mapping[str, Any]
    lg_metadata: Mapping[str, Any]
    state_digest: str = ""
    whitelist: tuple[str, ...] = ()
    source_path: str = ""
