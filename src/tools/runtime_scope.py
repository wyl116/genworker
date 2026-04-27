"""Request-scoped execution context for tool execution."""
from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping

from src.tools.security.models import EnforcementConstraint


_SCOPE_VAR: ContextVar["ExecutionScope | None"] = ContextVar(
    "genworker_execution_scope",
    default=None,
)
_TOOL_PIPELINE_VAR: ContextVar[Any | None] = ContextVar(
    "genworker_tool_pipeline",
    default=None,
)


@dataclass(frozen=True)
class ExecutionScope:
    """Runtime caller context carried through one routed execution."""

    tenant_id: str
    worker_id: str
    skill_id: str
    trust_gate: Any
    allowed_tool_names: frozenset[str]
    constraint: EnforcementConstraint = field(default_factory=EnforcementConstraint)
    scoped_tools: Mapping[str, Any] = field(default_factory=dict)


class ExecutionScopeProvider:
    """ContextVar-backed provider for the current execution scope."""

    def __init__(self) -> None:
        self._scope_var = _SCOPE_VAR

    def current(self) -> ExecutionScope | None:
        return self._scope_var.get()

    def require(self) -> ExecutionScope:
        scope = self.current()
        if scope is None:
            raise RuntimeError("Execution scope is not available")
        return scope

    @asynccontextmanager
    async def use(self, scope: ExecutionScope) -> AsyncIterator[ExecutionScope]:
        token: Token[ExecutionScope | None] = self._scope_var.set(scope)
        try:
            yield scope
        finally:
            self._scope_var.reset(token)


def current_execution_scope() -> ExecutionScope | None:
    """Return the current request-scoped execution scope, if any."""
    return _SCOPE_VAR.get()


def require_execution_scope() -> ExecutionScope:
    """Return the current execution scope or raise when none is set."""
    scope = current_execution_scope()
    if scope is None:
        raise RuntimeError("Execution scope is not available")
    return scope


def current_tool_pipeline() -> Any | None:
    """Return the current request-scoped ToolPipeline, if any."""
    return _TOOL_PIPELINE_VAR.get()


@asynccontextmanager
async def use_tool_pipeline(pipeline: Any) -> AsyncIterator[Any]:
    """Bind the active ToolPipeline to the current async context."""
    token: Token[Any | None] = _TOOL_PIPELINE_VAR.set(pipeline)
    try:
        yield pipeline
    finally:
        _TOOL_PIPELINE_VAR.reset(token)
