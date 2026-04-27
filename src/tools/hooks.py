"""
Tool hooks - Pre/post execution hooks for the ToolPipeline.

Hooks run OUTSIDE the middleware chain:
- pre_execute: Before middleware chain, can deny/warn/allow.
- post_execute: After execution, for audit/logging/etc.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from .formatters import ToolResult


class HookAction(str, Enum):
    """Hook decision action."""
    ALLOW = "allow"
    DENY = "deny"
    WARN = "warn"


@dataclass(frozen=True)
class HookResult:
    """Immutable result from a pre-execute hook."""
    action: HookAction
    message: str = ""


class ToolHook(Protocol):
    """
    Protocol for tool execution hooks.

    Implementations should be stateless or thread-safe.
    """

    async def pre_execute(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> HookResult:
        """
        Called before middleware chain.

        Returns HookResult with action:
        - ALLOW: proceed to middleware chain
        - DENY: short-circuit with error ToolResult
        - WARN: log warning and proceed
        """
        ...

    async def post_execute(
        self, tool_name: str, tool_input: dict[str, Any], result: ToolResult
    ) -> None:
        """Called after execution completes. For logging, auditing, etc."""
        ...
