"""
Tool sandbox - Pure function tool filtering and ScopedToolExecutor.

filter_tools(): Stateless pure function that computes available tools
    based on policy mode (blacklist/whitelist) and optional tenant overlay.

ScopedToolExecutor: Terminal executor in the ToolPipeline. Checks tool
    membership in allowed_tools and returns PermissionDenial data object
    (NOT exception) when denied.
"""
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from src.common.logger import get_logger

from .formatters import ToolResult
from .mcp.tool import Tool

logger = get_logger()


# --- Data Objects ---

@dataclass(frozen=True)
class ToolPolicy:
    """
    Immutable tool access policy.

    mode: "blacklist" (deny listed) or "whitelist" (allow listed only).
    denied_tools: Tool names to deny (blacklist mode).
    allowed_tools: Tool names to allow (whitelist mode).
    """
    mode: str = "blacklist"
    denied_tools: frozenset[str] = field(default_factory=frozenset)
    allowed_tools: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class TenantPolicy:
    """Tenant-level security overlay - additional denied tools."""
    denied_tools: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class PermissionDenial:
    """
    Permission denial as a data object (NOT an exception).

    tool_name: The tool that was denied.
    reason: Human-readable denial reason.
    context: Which layer denied it (hook / middleware / sandbox).
    """
    tool_name: str
    reason: str
    context: str = ""


# --- Pure Function ---

def filter_tools(
    all_tools: Sequence[Tool],
    policy: ToolPolicy,
    tenant_policy: TenantPolicy | None = None,
) -> tuple[Tool, ...]:
    """
    Pure function: compute available tools from policy.

    No side effects, no dependencies on Worker or any external state.

    Args:
        all_tools: All registered tools.
        policy: Worker-level tool policy (blacklist or whitelist).
        tenant_policy: Optional tenant-level denied tools overlay.

    Returns:
        Tuple of allowed Tool objects.
    """
    if policy.mode == "whitelist":
        available = tuple(
            t for t in all_tools if t.name in policy.allowed_tools
        )
    else:
        # Default: blacklist mode
        available = tuple(
            t for t in all_tools if t.name not in policy.denied_tools
        )

    # Tenant overlay
    if tenant_policy and tenant_policy.denied_tools:
        available = tuple(
            t for t in available if t.name not in tenant_policy.denied_tools
        )

    return available


# --- Terminal Executor ---

class ScopedToolExecutor:
    """
    Terminal executor in the ToolPipeline.

    Checks that the requested tool is within the allowed set,
    executes it, and formats the result as ToolResult.
    Returns PermissionDenial wrapped in ToolResult on denial.
    """

    def __init__(
        self,
        allowed_tools: Mapping[str, Tool],
    ):
        self._allowed_tools = dict(allowed_tools)

    @property
    def allowed_tool_names(self) -> frozenset[str]:
        return frozenset(self._allowed_tools.keys())

    @property
    def allowed_tools(self) -> Mapping[str, Tool]:
        return dict(self._allowed_tools)

    async def execute(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool: Tool | None = None,
    ) -> ToolResult:
        """
        Execute a tool if allowed, otherwise return denial.

        Args:
            tool_name: Name of the tool to execute.
            tool_input: Input parameters for the tool.

        Returns:
            ToolResult with execution output or denial message.
        """
        tool = tool or self._allowed_tools.get(tool_name)

        if tool is None:
            denial = PermissionDenial(
                tool_name=tool_name,
                reason=f"Tool '{tool_name}' is not in the allowed tool set",
                context="sandbox",
            )
            logger.warning(
                f"[ScopedToolExecutor] Permission denied: {denial.reason}"
            )
            return ToolResult(content=denial.reason, is_error=True)

        try:
            result = await self._invoke_handler(tool, tool_input)
            return self._format_result(result)
        except Exception as e:
            logger.error(
                f"[ScopedToolExecutor] Tool '{tool_name}' execution error: {e}",
                exc_info=True,
            )
            return ToolResult.from_error(e)

    async def _invoke_handler(
        self, tool: Tool, tool_input: dict[str, Any]
    ) -> Any:
        """Invoke the tool handler (supports sync and async)."""
        if inspect.iscoroutinefunction(tool.handler):
            return await tool.handler(**tool_input)
        return tool.handler(**tool_input)

    def _format_result(self, raw: Any) -> ToolResult:
        """Format raw handler output into ToolResult."""
        if isinstance(raw, ToolResult):
            return raw
        if isinstance(raw, str):
            return ToolResult.from_text(raw)
        if isinstance(raw, dict):
            import json
            return ToolResult.from_text(json.dumps(raw, ensure_ascii=False, indent=2))
        return ToolResult.from_text(str(raw))
