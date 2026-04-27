"""Builtin execute_code tool."""
from __future__ import annotations

import re
from typing import Any

from src.tools.formatters import ToolResult
from src.tools.mcp.tool import Tool
from src.tools.mcp.types import ConcurrencyLevel, MCPCategory, RiskLevel, ToolType
from src.tools.runtime_scope import current_execution_scope, current_tool_pipeline

from .code_rpc_bridge import resolve_script_callable_tools, run_code_in_sandbox
from .code_sandbox_config import get_code_execution_limits
from .registry import builtin_tool

_ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(api[_-]?key|token|secret|password|credentials?)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)Bearer\s+\S+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)://[^:]+:[^@]+@"), "://[REDACTED]@"),
)
_TRUNCATION_MARKER = "\n... [output truncated] ...\n"


@builtin_tool()
def create_execute_code_tool() -> Tool:
    """Register the execute_code tool."""

    async def _handler(
        code: str,
        enabled_tools: list[str] | None = None,
        timeout_seconds: int = 300,
        max_tool_calls: int | None = None,
    ) -> ToolResult:
        scope = current_execution_scope()
        pipeline = current_tool_pipeline()
        if scope is None or pipeline is None:
            return ToolResult(
                content="execute_code requires an active execution scope and tool pipeline",
                is_error=True,
            )

        limits = get_code_execution_limits()
        timeout_value = _clamp_timeout(timeout_seconds, limits.default_timeout_seconds, limits.max_timeout_seconds)
        tool_call_limit = _clamp_limit(
            max_tool_calls,
            limits.max_tool_calls,
            limits.max_tool_calls,
        )
        script_tools = resolve_script_callable_tools(
            parent_scope=scope,
            pipeline=pipeline,
            enabled_tools=enabled_tools,
        )
        outcome = await run_code_in_sandbox(
            code=code,
            parent_scope=scope,
            pipeline=pipeline,
            enabled_tools=script_tools,
            timeout_seconds=timeout_value,
            max_tool_calls=tool_call_limit,
        )
        stdout = _sanitize_text(outcome.stdout)
        stderr = _sanitize_text(outcome.stderr)
        content, truncated = _truncate_head_tail(stdout, limits.output_bytes)
        return ToolResult(
            content=content,
            is_error=outcome.status != "success",
            truncated=truncated or outcome.truncated,
            original_length=len(stdout),
            metadata={
                "status": outcome.status,
                "tool_calls_made": outcome.tool_calls_made,
                "duration_seconds": outcome.duration_seconds,
                "stderr_tail": stderr[-4000:],
            },
        )

    return Tool(
        name="execute_code",
        description=(
            "Execute Python code in an isolated subprocess. "
            "Import runtime tools from genworker_tools and print the processed result to stdout."
        ),
        handler=_handler,
        parameters={
            "code": {
                "type": "string",
                "description": (
                    "Python script source. Import tools via `from genworker_tools import ...` "
                    "and print the processed result to stdout."
                ),
            },
            "enabled_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional subset of tool names the script may call. "
                    "Defaults to the current run's allowed tool set."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Optional timeout in seconds. Default 300, max 600.",
            },
        },
        required_params=("code",),
        tool_type=ToolType.EXECUTE,
        category=MCPCategory.RESTRICTED,
        risk_level=RiskLevel.HIGH,
        concurrency=ConcurrencyLevel.EXCLUSIVE,
        tags=frozenset({"code", "sandbox", "rpc"}),
    )


def _sanitize_text(text: str) -> str:
    cleaned = _ANSI_RE.sub("", text or "")
    for pattern, replacement in _REDACTION_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def _clamp_timeout(value: Any, default: int, max_timeout: int) -> int:
    return _clamp_limit(value, default, max_timeout)


def _clamp_limit(value: Any, default: int, max_value: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    if limit <= 0:
        limit = default
    return min(limit, max_value)


def _truncate_head_tail(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False

    marker = _TRUNCATION_MARKER.encode("utf-8")
    budget = max(max_bytes - len(marker), 0)
    head_budget = int(budget * 0.4)
    tail_budget = budget - head_budget
    head = encoded[:head_budget].decode("utf-8", errors="ignore")
    tail = encoded[-tail_budget:].decode("utf-8", errors="ignore") if tail_budget else ""
    return f"{head}{_TRUNCATION_MARKER}{tail}", True
