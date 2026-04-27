"""Filesystem-backed registry for reusable script tools."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.common.settings import get_settings
from src.common.logger import get_logger
from src.tools.mcp.server import MCPServer
from src.tools.mcp.tool import Tool

from .script_tool import build_script_tool

logger = get_logger()


@dataclass(frozen=True)
class ScriptToolSpec:
    name: str
    description: str
    script_source: str
    enabled_rpc_tools: tuple[str, ...]
    parameters: dict[str, Any]
    visible_to_llm: bool = False
    timeout_seconds: int = 300


class ScriptToolRegistry:
    """Loads reusable script tools from YAML files and syncs them into MCP."""

    def __init__(self, directory: str | Path | None = None) -> None:
        resolved = directory or get_settings().script_tool_dir
        self._directory = Path(str(resolved)).expanduser()
        self._snapshot: dict[str, int] = {}
        self._registered_names: set[str] = set()

    @property
    def directory(self) -> Path:
        return self._directory

    def sync_to_server(self, server: MCPServer) -> None:
        """Reload tools when backing files change and update the MCP server."""
        snapshot = self._compute_snapshot()
        if snapshot == self._snapshot and self._registered_names:
            return

        tools = self.load_tools()
        new_names = {tool.name for tool in tools}
        for name in sorted(self._registered_names - new_names):
            server.unregister_tool(name)
        for tool in tools:
            server.register_tool(tool)

        self._registered_names = new_names
        self._snapshot = snapshot

    def load_tools(self) -> tuple[Tool, ...]:
        """Load all script tool definitions from disk."""
        if not self._directory.is_dir():
            return ()

        tools: list[Tool] = []
        for path in sorted(self._directory.glob("*.yaml")):
            try:
                spec = self._load_spec(path)
            except Exception as exc:
                logger.warning("[ScriptToolRegistry] Failed to load %s: %s", path, exc)
                continue
            tools.append(
                build_script_tool(
                    name=spec.name,
                    script_source=spec.script_source,
                    enabled_rpc_tools=spec.enabled_rpc_tools,
                    description=spec.description,
                    parameters=spec.parameters,
                    visible_to_llm=spec.visible_to_llm,
                    timeout_seconds=spec.timeout_seconds,
                )
            )
        return tuple(tools)

    def _compute_snapshot(self) -> dict[str, int]:
        if not self._directory.is_dir():
            return {}
        return {
            str(path): path.stat().st_mtime_ns
            for path in sorted(self._directory.glob("*.yaml"))
            if path.is_file()
        }

    def _load_spec(self, path: Path) -> ScriptToolSpec:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"script tool file {path} must contain a mapping")

        name = str(raw.get("name", "") or "").strip()
        if not name:
            raise ValueError(f"script tool file {path} missing 'name'")
        script_source = str(raw.get("script_source", "") or "")
        if not script_source.strip():
            raise ValueError(f"script tool file {path} missing 'script_source'")
        parameters = raw.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ValueError(f"script tool file {path} has invalid 'parameters'")

        enabled_rpc_tools_raw = raw.get("enabled_rpc_tools", ())
        if isinstance(enabled_rpc_tools_raw, str):
            enabled_rpc_tools_raw = [enabled_rpc_tools_raw]

        spec = ScriptToolSpec(
            name=name,
            description=str(raw.get("description", "") or ""),
            script_source=script_source,
            enabled_rpc_tools=tuple(
                str(item).strip()
                for item in enabled_rpc_tools_raw
                if str(item).strip()
            ),
            parameters=dict(parameters),
            visible_to_llm=bool(raw.get("visible_to_llm", False)),
            timeout_seconds=int(raw.get("timeout_seconds", 300) or 300),
        )
        logger.debug("[ScriptToolRegistry] Loaded %s from %s", spec.name, path)
        return spec
