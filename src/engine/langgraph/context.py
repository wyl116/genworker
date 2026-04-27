"""Node execution context passed into langgraph builders and handlers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from src.engine.protocols import LLMClient, ToolExecutor
from src.engine.state import UsageBudget, WorkerContext
from src.services.llm.intent import LLMCallIntent


@dataclass(frozen=True)
class NodeContext:
    """Dependencies exposed to declarative and Python graph nodes."""

    worker_context: WorkerContext
    tools: ToolExecutor
    llm: LLMClient
    checkpointer: Any
    instruction_resolver: Callable[[str], str]
    intent_resolver: Callable[[str], LLMCallIntent]
    budget: UsageBudget
    tenant_id: str
    worker_id: str
    skill_id: str
    thread_id: str
    available_tools: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def instruction(self, ref: str) -> str:
        return self.instruction_resolver(ref)

    def intent(self, ref: str) -> LLMCallIntent:
        return self.intent_resolver(ref)

    def tool_schemas(self, names: tuple[str, ...]) -> list[dict[str, Any]]:
        if not names:
            return list(self.available_tools)
        wanted = set(names)
        return [
            schema
            for schema in self.available_tools
            if schema.get("function", {}).get("name", "") in wanted
        ]
