"""
Glob tool - Find files by glob pattern within the workspace.

Returns matching file paths sorted by modification time (newest first).
Results are relative to workspace root.
"""
import os
from pathlib import Path

from src.common.logger import get_logger

from .registry import builtin_tool
from .workspace_sandbox import WorkspaceSandboxError, resolve_workspace_path
from ..mcp.tool import Tool
from ..mcp.types import ConcurrencyLevel, MCPCategory, RiskLevel, ToolType

logger = get_logger()

MAX_RESULTS = 200


async def _glob_handler(
    pattern: str,
    path: str = "",
    *,
    _workspace_root: str,
) -> str:
    """Find files matching a glob pattern in the workspace."""
    try:
        if path:
            search_dir = resolve_workspace_path(path, _workspace_root)
        else:
            search_dir = os.path.realpath(_workspace_root)
    except WorkspaceSandboxError as e:
        return f"Error: {e}"

    if not os.path.isdir(search_dir):
        return f"Error: Directory not found: {path or _workspace_root}"

    try:
        search_path = Path(search_dir)
        matches = list(search_path.glob(pattern))

        # Filter: only files, within workspace
        real_root = os.path.realpath(_workspace_root)
        valid = []
        for m in matches:
            real_m = str(m.resolve())
            if m.is_file() and real_m.startswith(real_root):
                valid.append(m)

        # Sort by modification time (newest first)
        valid.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        truncated = len(valid) > MAX_RESULTS
        display = valid[:MAX_RESULTS]

        # Relative paths
        rel_paths = [
            str(p.relative_to(search_path)) for p in display
        ]

        header = f"Found {len(valid)} file(s) matching '{pattern}'"
        if truncated:
            header += f" (showing first {MAX_RESULTS})"

        if not rel_paths:
            return f"{header}\n(no matches)"

        body = "\n".join(rel_paths)
        logger.info(f"[Glob] '{pattern}': {len(valid)} matches")
        return f"{header}\n{body}"

    except Exception as e:
        logger.error(f"[Glob] Failed for pattern '{pattern}': {e}")
        return f"Error: {e}"


@builtin_tool(requires=("workspace_root",))
def create_glob_tool(workspace_root: str) -> Tool:
    """Create the glob_search Tool instance."""

    async def handler(pattern: str, path: str = "") -> str:
        return await _glob_handler(
            pattern, path, _workspace_root=workspace_root,
        )

    return Tool(
        name="glob_search",
        description=(
            "Find files by glob pattern within the workspace. "
            "Examples: '*.py', '**/*.json', 'data/*.csv'. "
            "Returns file paths sorted by modification time (newest first)."
        ),
        handler=handler,
        parameters={
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match (e.g., '**/*.py', 'data/*.csv')",
            },
            "path": {
                "type": "string",
                "description": "Subdirectory to search in (default: workspace root)",
            },
        },
        required_params=("pattern",),
        tool_type=ToolType.SEARCH,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
        concurrency=ConcurrencyLevel.SAFE,
        tags=frozenset({"glob", "search", "files", "workspace"}),
    )
