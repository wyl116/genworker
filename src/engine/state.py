"""
Engine state models - GraphState, UsageBudget, StepResult.

All models are frozen dataclasses for immutability.
"""
import json
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class UsageBudget:
    """
    Token budget tracker.

    ReactEngine accumulates used_tokens from each LLM response.
    When used_tokens exceeds max_tokens, the engine yields
    BudgetExceededEvent and stops (no exception thrown).
    """
    max_tokens: int = 0
    used_tokens: int = 0

    @property
    def exceeded(self) -> bool:
        """Check if budget is exceeded (0 means unlimited)."""
        if self.max_tokens <= 0:
            return False
        return self.used_tokens >= self.max_tokens

    @property
    def remaining(self) -> int:
        """Remaining tokens (0 means unlimited)."""
        if self.max_tokens <= 0:
            return 0
        return max(0, self.max_tokens - self.used_tokens)

    def add_usage(self, tokens: int) -> "UsageBudget":
        """Return a new UsageBudget with added token usage."""
        return UsageBudget(
            max_tokens=self.max_tokens,
            used_tokens=self.used_tokens + tokens,
        )


@dataclass(frozen=True)
class StepResult:
    """
    Unified step result for data passing between workflow/hybrid steps.

    autonomous steps produce content (free text).
    deterministic steps may produce structured_data (JSON).
    Next step receives previous_result.as_input.
    """
    step_name: str
    step_type: str  # "autonomous" | "deterministic" | "input"
    content: str
    structured_data: dict[str, Any] | None = None
    success: bool = True
    error: str | None = None

    @property
    def as_input(self) -> str:
        """
        Format as input for the next step.

        Prefers JSON representation of structured_data,
        falls back to content text.
        """
        if self.structured_data:
            return json.dumps(self.structured_data, ensure_ascii=False)
        return self.content


@dataclass(frozen=True)
class WorkerContext:
    """
    Minimal worker context passed to engines.

    Engines use this for prompt building and tool sandbox access.
    The full Worker model lives in the worker layer; this is the
    subset engines need.
    """
    worker_id: str = ""
    tenant_id: str = ""
    skill_id: str = ""
    identity: str = ""
    principles: str = ""
    constraints: str = ""
    directives: str = ""
    learned_rules: str = ""
    historical_context: str = ""
    task_context: str = ""
    contact_context: str = ""
    tool_names: tuple[str, ...] = ()
    available_skill_ids: tuple[str, ...] = ()
    memory_orchestrator: Any | None = None
    worker_dir: str = ""
    trust_gate: Any | None = None
    goal_default_pre_script: Any | None = None


@dataclass(frozen=True)
class GraphState:
    """
    Execution graph state.

    Carries messages, worker context, and budget through the
    execution loop. Immutable - create new instances on each round.
    """
    messages: tuple[dict[str, Any], ...] = ()
    worker_context: WorkerContext = WorkerContext()
    budget: UsageBudget = UsageBudget()
    thread_id: str = ""

    def append_message(self, message: dict[str, Any]) -> "GraphState":
        """Return a new GraphState with the message appended."""
        return GraphState(
            messages=(*self.messages, message),
            worker_context=self.worker_context,
            budget=self.budget,
            thread_id=self.thread_id,
        )

    def append_messages(self, new_messages: list[dict[str, Any]]) -> "GraphState":
        """Return a new GraphState with messages appended."""
        return GraphState(
            messages=(*self.messages, *new_messages),
            worker_context=self.worker_context,
            budget=self.budget,
            thread_id=self.thread_id,
        )

    def with_budget(self, budget: UsageBudget) -> "GraphState":
        """Return a new GraphState with updated budget."""
        return GraphState(
            messages=self.messages,
            worker_context=self.worker_context,
            budget=budget,
            thread_id=self.thread_id,
        )
