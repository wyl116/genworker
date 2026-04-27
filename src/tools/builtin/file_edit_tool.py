"""
File edit tool - String replacement editing in workspace files.

Requires exact match of old_string. By default, old_string must appear
exactly once (unique match). Use replace_all=True for multiple replacements.
"""
import os

from src.common.logger import get_logger

from .registry import builtin_tool
from .workspace_sandbox import WorkspaceSandboxError, resolve_workspace_path
from ..mcp.tool import Tool
from ..mcp.types import ConcurrencyLevel, MCPCategory, RiskLevel, ToolType

logger = get_logger()


async def _file_edit_handler(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    *,
    _workspace_root: str,
) -> str:
    """Edit a file by replacing old_string with new_string."""
    try:
        resolved = resolve_workspace_path(file_path, _workspace_root)
    except WorkspaceSandboxError as e:
        return f"Error: {e}"

    if not os.path.exists(resolved):
        return f"Error: File not found: {file_path}"

    if not os.path.isfile(resolved):
        return f"Error: Not a file: {file_path}"

    if old_string == new_string:
        return "Error: old_string and new_string are identical"

    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        count = content.count(old_string)

        if count == 0:
            return (
                f"Error: old_string not found in {file_path}. "
                "Ensure the string matches exactly (including whitespace)."
            )

        if count > 1 and not replace_all:
            return (
                f"Error: old_string found {count} times in {file_path}. "
                "Use replace_all=true to replace all occurrences, "
                "or provide a more specific old_string."
            )

        new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)

        with open(resolved, "w", encoding="utf-8") as f:
            f.write(new_content)

        replacements = count if replace_all else 1
        logger.info(
            f"[FileEdit] {file_path}: {replacements} replacement(s)"
        )

        return (
            f"Edited: {file_path}\n"
            f"Replacements: {replacements}"
        )

    except Exception as e:
        logger.error(f"[FileEdit] Failed to edit {file_path}: {e}")
        return f"Error editing file: {e}"


@builtin_tool(requires=("workspace_root",))
def create_file_edit_tool(workspace_root: str) -> Tool:
    """Create the file_edit Tool instance."""

    async def handler(
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        return await _file_edit_handler(
            file_path, old_string, new_string, replace_all,
            _workspace_root=workspace_root,
        )

    return Tool(
        name="file_edit",
        description=(
            "Edit a file by replacing old_string with new_string. "
            "old_string must match exactly and appear only once "
            "(unless replace_all=true). Use for targeted modifications."
        ),
        handler=handler,
        parameters={
            "file_path": {
                "type": "string",
                "description": "Path to the file (relative to workspace root)",
            },
            "old_string": {
                "type": "string",
                "description": "Exact text to find and replace",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences (default: false)",
            },
        },
        required_params=("file_path", "old_string", "new_string"),
        tool_type=ToolType.WRITE,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.MEDIUM,
        concurrency=ConcurrencyLevel.PATH_SCOPED,
        resource_key_param="file_path",
        tags=frozenset({"file", "edit", "replace", "workspace"}),
    )
