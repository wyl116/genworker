"""Execution helpers for task-level pre-scripts."""
from __future__ import annotations

from src.tools.pipeline import ToolCallContext, ToolPipeline
from src.tools.runtime_scope import ExecutionScope

from .models import InlineScript, PreScript, ScriptRef


async def run_pre_script(
    *,
    pre_script: PreScript,
    scope: ExecutionScope,
    pipeline: ToolPipeline,
) -> str:
    """Execute one pre-script through the standard ToolPipeline."""
    tool_name: str
    tool_input: dict[str, object]
    if isinstance(pre_script, InlineScript):
        tool_name = "execute_code"
        tool_input = {
            "code": pre_script.source,
            "enabled_tools": list(pre_script.enabled_tools),
            "timeout_seconds": pre_script.timeout_seconds,
            "max_tool_calls": pre_script.max_tool_calls,
        }
    elif isinstance(pre_script, ScriptRef):
        tool_name = pre_script.tool_name
        tool_input = pre_script.input_dict
    else:
        raise TypeError(f"Unsupported pre_script type: {type(pre_script)!r}")

    tool = scope.scoped_tools.get(tool_name) or getattr(
        pipeline.executor, "allowed_tools", {}
    ).get(tool_name)
    result = await pipeline.execute(
        ToolCallContext.from_scope(
            scope,
            tool_name=tool_name,
            tool_input=tool_input,
            risk_level=str(getattr(tool, "risk_level", "high")),
            tool=tool,
            step_name="pre_script",
        )
    )
    if result.is_error:
        metadata = dict(result.metadata or {})
        detail = (
            str(metadata.get("stderr_tail", "")).strip()
            or result.content.strip()
            or "pre_script failed"
        )
        return f"[pre_script error: {detail}]"
    return result.content
