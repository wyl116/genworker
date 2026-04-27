"""
File read tool - Read files from the workspace with line numbers.

Supports offset/limit pagination for large files.
Binary files are detected and rejected.
"""
import os

from src.common.logger import get_logger

from .registry import builtin_tool
from .workspace_sandbox import WorkspaceSandboxError, resolve_workspace_path
from ..mcp.tool import Tool
from ..mcp.types import ConcurrencyLevel, MCPCategory, RiskLevel, ToolType

logger = get_logger()

DEFAULT_LIMIT = 2000
MAX_LIMIT = 10000

# Bytes threshold for binary detection (check first 8KB)
_BINARY_CHECK_SIZE = 8192


def _is_binary(path: str) -> bool:
    """Detect binary files by checking for null bytes."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(_BINARY_CHECK_SIZE)
        return b"\x00" in chunk
    except OSError:
        return False


async def _file_read_handler(
    file_path: str,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    *,
    _workspace_root: str,
) -> str:
    """Read file with line numbers, offset, and limit."""
    try:
        resolved = resolve_workspace_path(file_path, _workspace_root)
    except WorkspaceSandboxError as e:
        return f"Error: {e}"

    if not os.path.exists(resolved):
        return f"Error: File not found: {file_path}"

    if not os.path.isfile(resolved):
        return f"Error: Not a file: {file_path}"

    if _is_binary(resolved):
        return f"Error: Binary file cannot be displayed: {file_path}"

    limit = min(max(1, limit), MAX_LIMIT)
    offset = max(0, offset)

    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        total_lines = len(all_lines)
        selected = all_lines[offset: offset + limit]
        start_line = offset + 1

        numbered = []
        for i, line in enumerate(selected):
            line_no = start_line + i
            numbered.append(f"{line_no}\t{line.rstrip()}")

        header = (
            f"File: {file_path} | "
            f"Lines {start_line}-{start_line + len(selected) - 1} "
            f"of {total_lines}"
        )

        if not selected:
            return f"{header}\n(no content in range)"

        content = "\n".join(numbered)
        logger.info(
            f"[FileRead] {file_path}: lines {start_line}-"
            f"{start_line + len(selected) - 1}/{total_lines}"
        )
        return f"{header}\n{content}"

    except Exception as e:
        logger.error(f"[FileRead] Failed to read {file_path}: {e}")
        return f"Error reading file: {e}"


@builtin_tool(requires=("workspace_root",))
def create_file_read_tool(workspace_root: str) -> Tool:
    """Create the file_read Tool instance."""

    async def handler(
        file_path: str,
        offset: int = 0,
        limit: int = DEFAULT_LIMIT,
    ) -> str:
        return await _file_read_handler(
            file_path, offset, limit, _workspace_root=workspace_root,
        )

    return Tool(
        name="file_read",
        description=(
            "Read a file from the workspace with line numbers. "
            "Supports offset and limit for pagination of large files. "
            "Returns numbered lines in format: {line_number}\\t{content}"
        ),
        handler=handler,
        parameters={
            "file_path": {
                "type": "string",
                "description": "Path to the file (relative to workspace root)",
            },
            "offset": {
                "type": "integer",
                "description": "Starting line index (0-based, default: 0)",
            },
            "limit": {
                "type": "integer",
                "description": f"Max lines to read (default: {DEFAULT_LIMIT}, max: {MAX_LIMIT})",
            },
        },
        required_params=("file_path",),
        tool_type=ToolType.READ,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
        concurrency=ConcurrencyLevel.SAFE,
        tags=frozenset({"file", "read", "workspace"}),
    )
