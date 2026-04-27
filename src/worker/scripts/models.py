"""Data models for inline and referenced task pre-scripts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.tools.builtin.code_sandbox_config import get_code_execution_limits


@dataclass(frozen=True)
class InlineScript:
    """Inline Python source executed before the main task dispatch."""

    source: str
    enabled_tools: tuple[str, ...] = ()
    timeout_seconds: int = 300
    max_tool_calls: int = 50

    def __post_init__(self) -> None:
        size = len(self.source.encode("utf-8"))
        limit = get_code_execution_limits().inline_script_size_limit_bytes
        if size > limit:
            raise ValueError(
                f"InlineScript.source is {size}B; exceeds {limit}B limit."
            )


@dataclass(frozen=True)
class ScriptRef:
    """Reference to a reusable tool-based script."""

    tool_name: str
    tool_input: tuple[tuple[str, Any], ...] = ()

    @property
    def input_dict(self) -> dict[str, Any]:
        return dict(self.tool_input)


PreScript = InlineScript | ScriptRef


def serialize_pre_script(pre_script: PreScript | None) -> dict[str, Any] | None:
    """Serialize pre-script state into a JSON-safe mapping."""
    if pre_script is None:
        return None
    if isinstance(pre_script, InlineScript):
        return {
            "kind": "inline",
            "source": pre_script.source,
            "enabled_tools": list(pre_script.enabled_tools),
            "timeout_seconds": pre_script.timeout_seconds,
            "max_tool_calls": pre_script.max_tool_calls,
        }
    if isinstance(pre_script, ScriptRef):
        return {
            "kind": "ref",
            "tool_name": pre_script.tool_name,
            "tool_input": [[key, value] for key, value in pre_script.tool_input],
        }
    raise TypeError(f"unknown pre_script type: {type(pre_script)!r}")


def deserialize_pre_script(data: Any) -> PreScript | None:
    """Deserialize pre-script data from JSON/YAML metadata."""
    if not data:
        return None
    if not isinstance(data, dict):
        raise ValueError("pre_script must be an object")

    kind = str(data.get("kind", "") or "").strip().lower()
    if not kind:
        if "source" in data:
            kind = "inline"
        elif "tool_name" in data:
            kind = "ref"
    if kind == "inline":
        return InlineScript(
            source=str(data.get("source", "") or ""),
            enabled_tools=tuple(
                str(item).strip()
                for item in data.get("enabled_tools", ())
                if str(item).strip()
            ),
            timeout_seconds=int(data.get("timeout_seconds", 300) or 300),
            max_tool_calls=int(data.get("max_tool_calls", 50) or 50),
        )
    if kind == "ref":
        raw_input = data.get("tool_input", ())
        if isinstance(raw_input, dict):
            pairs = tuple((str(key), value) for key, value in raw_input.items())
        else:
            pairs = tuple(
                (str(item[0]), item[1])
                for item in raw_input
                if isinstance(item, (list, tuple)) and len(item) == 2
            )
        return ScriptRef(
            tool_name=str(data.get("tool_name", "") or ""),
            tool_input=pairs,
        )
    raise ValueError(f"unknown pre_script kind: {kind}")
