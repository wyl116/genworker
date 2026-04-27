"""
Grep tool - Search file contents with regex within the workspace.

Uses subprocess grep for performance with a Python re fallback.
Results formatted as {file}:{line_number}:{content}.
"""
import asyncio
import os
import re

from src.common.logger import get_logger

from .registry import builtin_tool
from .workspace_sandbox import WorkspaceSandboxError, resolve_workspace_path
from ..mcp.tool import Tool
from ..mcp.types import ConcurrencyLevel, MCPCategory, RiskLevel, ToolType

logger = get_logger()

DEFAULT_MAX_RESULTS = 100
_GREP_TIMEOUT = 10


async def _grep_handler(
    pattern: str,
    path: str = "",
    include: str = "",
    max_results: int = DEFAULT_MAX_RESULTS,
    *,
    _workspace_root: str,
) -> str:
    """Search file contents for a regex pattern."""
    try:
        if path:
            search_dir = resolve_workspace_path(path, _workspace_root)
        else:
            search_dir = os.path.realpath(_workspace_root)
    except WorkspaceSandboxError as e:
        return f"Error: {e}"

    if not os.path.isdir(search_dir):
        return f"Error: Directory not found: {path or _workspace_root}"

    max_results = min(max(1, max_results), 500)

    try:
        return await _grep_subprocess(
            pattern, search_dir, include, max_results, _workspace_root,
        )
    except FileNotFoundError:
        return _grep_python_fallback(
            pattern, search_dir, include, max_results, _workspace_root,
        )
    except Exception as e:
        logger.error(f"[Grep] Subprocess failed, trying fallback: {e}")
        return _grep_python_fallback(
            pattern, search_dir, include, max_results, _workspace_root,
        )


async def _grep_subprocess(
    pattern: str,
    search_dir: str,
    include: str,
    max_results: int,
    workspace_root: str,
) -> str:
    """Execute grep via subprocess."""
    cmd = ["grep", "-rn", "--color=never"]
    if include:
        cmd.extend(["--include", include])
    cmd.extend(["-m", str(max_results), "-E", pattern, search_dir])

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, _ = await asyncio.wait_for(
        process.communicate(), timeout=_GREP_TIMEOUT,
    )

    output = stdout_bytes.decode("utf-8", errors="replace").strip()
    if not output:
        return f"No matches found for pattern '{pattern}'"

    real_root = os.path.realpath(workspace_root)
    lines = output.split("\n")[:max_results]
    relative_lines = [
        line.replace(real_root + os.sep, "", 1) for line in lines
    ]

    header = f"Found matches for '{pattern}' ({len(relative_lines)} results)"
    return f"{header}\n" + "\n".join(relative_lines)


def _grep_python_fallback(
    pattern: str,
    search_dir: str,
    include: str,
    max_results: int,
    workspace_root: str,
) -> str:
    """Pure Python regex fallback when grep binary is unavailable."""
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        return f"Error: Invalid regex pattern: {e}"

    real_root = os.path.realpath(workspace_root)
    results: list[str] = []

    for root, _, files in os.walk(search_dir):
        for fname in files:
            if include and not _matches_glob(fname, include):
                continue

            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    for line_no, line in enumerate(f, 1):
                        if compiled.search(line):
                            rel = fpath.replace(real_root + os.sep, "", 1)
                            results.append(f"{rel}:{line_no}:{line.rstrip()}")
                            if len(results) >= max_results:
                                break
            except (OSError, UnicodeDecodeError):
                continue

            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break

    if not results:
        return f"No matches found for pattern '{pattern}'"

    header = f"Found matches for '{pattern}' ({len(results)} results)"
    return f"{header}\n" + "\n".join(results)


def _matches_glob(filename: str, glob_pattern: str) -> bool:
    """Simple glob matching for include filter."""
    import fnmatch
    return fnmatch.fnmatch(filename, glob_pattern)


@builtin_tool(requires=("workspace_root",))
def create_grep_tool(workspace_root: str) -> Tool:
    """Create the grep_search Tool instance."""

    async def handler(
        pattern: str,
        path: str = "",
        include: str = "",
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> str:
        return await _grep_handler(
            pattern, path, include, max_results,
            _workspace_root=workspace_root,
        )

    return Tool(
        name="grep_search",
        description=(
            "Search file contents with regex pattern within the workspace. "
            "Returns matching lines as {file}:{line}:{content}. "
            "Use 'include' to filter by file type (e.g., '*.py')."
        ),
        handler=handler,
        parameters={
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for (POSIX extended regex)",
            },
            "path": {
                "type": "string",
                "description": "Subdirectory to search in (default: workspace root)",
            },
            "include": {
                "type": "string",
                "description": "File glob filter (e.g., '*.py', '*.json')",
            },
            "max_results": {
                "type": "integer",
                "description": f"Maximum results to return (default: {DEFAULT_MAX_RESULTS}, max: 500)",
            },
        },
        required_params=("pattern",),
        tool_type=ToolType.SEARCH,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
        concurrency=ConcurrencyLevel.SAFE,
        tags=frozenset({"grep", "search", "regex", "content", "workspace"}),
    )
