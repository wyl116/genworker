"""
File write tool - Create or overwrite files in the workspace.

Creates parent directories as needed. Reports bytes written and line count.
"""
import os

from src.common.logger import get_logger

from .registry import builtin_tool
from .workspace_sandbox import WorkspaceSandboxError, resolve_workspace_path
from ..mcp.tool import Tool
from ..mcp.types import ConcurrencyLevel, MCPCategory, RiskLevel, ToolType

logger = get_logger()


async def _file_write_handler(
    file_path: str,
    content: str,
    *,
    _workspace_root: str,
) -> str:
    """Write content to a file in the workspace."""
    try:
        resolved = resolve_workspace_path(file_path, _workspace_root)
    except WorkspaceSandboxError as e:
        return f"Error: {e}"

    try:
        parent = os.path.dirname(resolved)
        os.makedirs(parent, exist_ok=True)

        is_new = not os.path.exists(resolved)

        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)

        bytes_written = len(content.encode("utf-8"))
        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        action = "Created" if is_new else "Updated"

        logger.info(
            f"[FileWrite] {action} {file_path}: "
            f"{bytes_written} bytes, {line_count} lines"
        )

        return (
            f"{action}: {file_path}\n"
            f"Bytes written: {bytes_written}\n"
            f"Lines: {line_count}"
        )

    except Exception as e:
        logger.error(f"[FileWrite] Failed to write {file_path}: {e}")
        return f"Error writing file: {e}"


@builtin_tool(requires=("workspace_root",))
def create_file_write_tool(workspace_root: str) -> Tool:
    """Create the file_write Tool instance."""

    async def handler(file_path: str, content: str) -> str:
        return await _file_write_handler(
            file_path, content, _workspace_root=workspace_root,
        )

    return Tool(
        name="file_write",
        description=(
            "Create or overwrite a file in the workspace. "
            "Parent directories are created automatically. "
            "Use file_edit for partial modifications."
        ),
        handler=handler,
        parameters={
            "file_path": {
                "type": "string",
                "description": "Path to the file (relative to workspace root)",
            },
            "content": {
                "type": "string",
                "description": "Full content to write to the file",
            },
        },
        required_params=("file_path", "content"),
        tool_type=ToolType.WRITE,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.MEDIUM,
        concurrency=ConcurrencyLevel.PATH_SCOPED,
        resource_key_param="file_path",
        tags=frozenset({"file", "write", "create", "workspace"}),
    )
