# edition: baseline
"""
Unit tests for workspace sandbox path resolution and containment.

Tests:
- Valid relative path resolves within workspace
- Valid absolute path within workspace passes
- Path traversal with ../ is blocked
- Empty path raises WorkspaceSandboxError
- Absolute path outside workspace is blocked
- Path equal to workspace root itself is allowed
"""
import os
from pathlib import Path

import pytest

from src.tools.builtin.workspace_sandbox import (
    WorkspaceSandboxError,
    resolve_workspace_path,
)


class TestResolveWorkspacePath:
    """Tests for resolve_workspace_path."""

    def test_valid_relative_path(self, tmp_path: Path) -> None:
        """Relative path resolves within workspace."""
        workspace = str(tmp_path)
        result = resolve_workspace_path("subdir/file.txt", workspace)
        expected = os.path.realpath(os.path.join(workspace, "subdir/file.txt"))
        assert result == expected

    def test_valid_absolute_path_within_workspace(self, tmp_path: Path) -> None:
        """Absolute path within workspace passes validation."""
        workspace = str(tmp_path)
        inner = os.path.join(workspace, "inner", "file.txt")
        result = resolve_workspace_path(inner, workspace)
        assert result == os.path.realpath(inner)

    def test_path_traversal_blocked(self, tmp_path: Path) -> None:
        """Path traversal with ../ raises WorkspaceSandboxError."""
        workspace = str(tmp_path / "workspace")
        os.makedirs(workspace, exist_ok=True)
        with pytest.raises(WorkspaceSandboxError, match="escapes workspace"):
            resolve_workspace_path("../outside.txt", workspace)

    def test_empty_path_raises_error(self, tmp_path: Path) -> None:
        """Empty path raises WorkspaceSandboxError."""
        workspace = str(tmp_path)
        with pytest.raises(WorkspaceSandboxError, match="must not be empty"):
            resolve_workspace_path("", workspace)

    def test_whitespace_only_path_raises_error(self, tmp_path: Path) -> None:
        """Whitespace-only path raises WorkspaceSandboxError."""
        workspace = str(tmp_path)
        with pytest.raises(WorkspaceSandboxError, match="must not be empty"):
            resolve_workspace_path("   ", workspace)

    def test_absolute_path_outside_workspace_blocked(self, tmp_path: Path) -> None:
        """Absolute path outside workspace raises WorkspaceSandboxError."""
        workspace = str(tmp_path / "workspace")
        os.makedirs(workspace, exist_ok=True)
        outside = str(tmp_path / "outside" / "secret.txt")
        with pytest.raises(WorkspaceSandboxError, match="escapes workspace"):
            resolve_workspace_path(outside, workspace)

    def test_workspace_root_itself_is_allowed(self, tmp_path: Path) -> None:
        """Path equal to workspace root itself is allowed."""
        workspace = str(tmp_path)
        result = resolve_workspace_path(workspace, workspace)
        assert result == os.path.realpath(workspace)

    def test_nested_traversal_blocked(self, tmp_path: Path) -> None:
        """Deeply nested traversal still blocked."""
        workspace = str(tmp_path / "workspace")
        os.makedirs(workspace, exist_ok=True)
        with pytest.raises(WorkspaceSandboxError, match="escapes workspace"):
            resolve_workspace_path("a/b/../../..", workspace)
