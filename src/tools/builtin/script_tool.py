"""Reusable L3 script tool support."""
from __future__ import annotations

import json
import re
from typing import Any, Mapping

from src.tools.mcp.tool import Tool
from src.tools.mcp.types import ConcurrencyLevel, MCPCategory, RiskLevel, ToolType
from src.tools.runtime_scope import current_execution_scope, current_tool_pipeline
from src.worker.tool_scope import LLM_HIDDEN_TAG

from .code_rpc_bridge import resolve_script_callable_tools, run_code_in_sandbox
from .code_sandbox_config import get_code_execution_limits
from .execute_code_tool import _clamp_timeout, _sanitize_text, _truncate_head_tail

_SCRIPT_INPUT_PATTERN = re.compile(r"[^A-Z0-9_]")


def build_script_tool(
    *,
    name: str,
    script_source: str,
    enabled_rpc_tools: tuple[str, ...] = (),
    description: str = "",
    parameters: Mapping[str, Any] | None = None,
    visible_to_llm: bool = False,
    timeout_seconds: int = 300,
) -> Tool:
    """Create a reusable script-backed tool that delegates to execute_code."""

    async def _handler(**tool_input) -> Any:
        scope = current_execution_scope()
        pipeline = current_tool_pipeline()
        if scope is None or pipeline is None:
            from src.tools.formatters import ToolResult

            return ToolResult(
                content="script tool requires an active execution scope and tool pipeline",
                is_error=True,
            )

        limits = get_code_execution_limits()
        timeout_value = _clamp_timeout(
            timeout_seconds,
            limits.default_timeout_seconds,
            limits.max_timeout_seconds,
        )
        enabled_tools = resolve_script_callable_tools(
            parent_scope=scope,
            pipeline=pipeline,
            enabled_tools=list(enabled_rpc_tools) if enabled_rpc_tools else None,
        )
        outcome = await run_code_in_sandbox(
            code=_wrap_script_source(script_source),
            parent_scope=scope,
            pipeline=pipeline,
            enabled_tools=enabled_tools,
            timeout_seconds=timeout_value,
            max_tool_calls=limits.max_tool_calls,
            extra_env=_build_script_input_env(tool_input),
        )
        from src.tools.formatters import ToolResult

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

    tags = {"code", "script"}
    if not visible_to_llm:
        tags.add(LLM_HIDDEN_TAG)
    return Tool(
        name=name,
        description=description or f"Reusable script tool '{name}'",
        handler=_handler,
        parameters=dict(parameters or {}),
        required_params=tuple(),
        tool_type=ToolType.EXECUTE,
        category=MCPCategory.SPECIALIZED,
        risk_level=RiskLevel.HIGH,
        concurrency=ConcurrencyLevel.EXCLUSIVE,
        tags=frozenset(tags),
    )


def _wrap_script_source(script_source: str) -> str:
    header = (
        "import json\n"
        "import os\n\n"
        "SCRIPT_INPUTS = json.loads(os.environ.get('LITTLEWANG_SCRIPT_INPUTS_JSON', '{}'))\n\n"
    )
    return f"{header}{script_source}"


def _build_script_input_env(tool_input: Mapping[str, Any]) -> dict[str, str]:
    env: dict[str, str] = {
        "LITTLEWANG_SCRIPT_INPUTS_JSON": json.dumps(tool_input, ensure_ascii=False),
    }
    for key, value in tool_input.items():
        env_key = "LITTLEWANG_SCRIPT_INPUT_" + _SCRIPT_INPUT_PATTERN.sub(
            "_", str(key).upper()
        )
        env[env_key] = json.dumps(value, ensure_ascii=False)
    return env
