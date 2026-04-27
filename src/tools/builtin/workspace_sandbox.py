"""
Workspace sandbox - Path containment for file/search tools.

Ensures all file operations stay within the designated workspace
directory. Uses os.path.realpath() to resolve symlinks and prevent
path traversal attacks.
"""
import os


class WorkspaceSandboxError(Exception):
    """Raised when a path escapes the workspace sandbox."""
    pass


def resolve_workspace_path(
    file_path: str,
    workspace_root: str,
) -> str:
    """
    Resolve and validate that file_path is within workspace_root.

    Handles:
    - Relative paths (resolved against workspace_root)
    - Absolute paths (must still be within workspace_root)
    - Symlinks (resolved via realpath before check)
    - Path traversal (../ sequences)

    Args:
        file_path: User-provided file path (relative or absolute).
        workspace_root: Root directory of the workspace sandbox.

    Returns:
        Resolved absolute path guaranteed to be within workspace_root.

    Raises:
        WorkspaceSandboxError: If path escapes workspace or is invalid.
    """
    if not file_path or not file_path.strip():
        raise WorkspaceSandboxError("File path must not be empty")

    real_root = os.path.realpath(workspace_root)

    if os.path.isabs(file_path):
        resolved = os.path.realpath(file_path)
    else:
        resolved = os.path.realpath(os.path.join(real_root, file_path))

    if not resolved.startswith(real_root + os.sep) and resolved != real_root:
        raise WorkspaceSandboxError(
            f"Path '{file_path}' escapes workspace sandbox"
        )

    return resolved
