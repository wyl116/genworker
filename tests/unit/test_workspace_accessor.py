# edition: baseline
"""
Unit tests for WorkspaceAccessor - path sandbox and write permission zones.
"""
import pytest
from pathlib import Path

from src.worker.data_access.models import DataSpaceConfig
from src.worker.data_access.workspace_accessor import (
    WorkspaceAccessor,
    PathEscapeError,
    WritePermissionError,
)


@pytest.fixture
def workspace(tmp_path: Path) -> WorkspaceAccessor:
    """Create a WorkspaceAccessor with temp directory structure."""
    data_dir = tmp_path / "data"
    for sub in ("uploads", "outputs", "scratch", "refs"):
        (data_dir / sub).mkdir(parents=True)
    (data_dir / "uploads" / "file.txt").write_bytes(b"upload content")
    (data_dir / "refs" / "shared.txt").write_bytes(b"shared content")
    config = DataSpaceConfig(root="data/")
    return WorkspaceAccessor(tmp_path, config)


class TestResolvePath:
    """Tests for resolve_path - path escape detection."""

    def test_valid_path_resolves(self, workspace: WorkspaceAccessor) -> None:
        resolved = workspace.resolve_path("uploads/file.txt")
        assert resolved.exists()
        assert resolved.name == "file.txt"

    def test_path_escape_with_dotdot_rejected(
        self, workspace: WorkspaceAccessor,
    ) -> None:
        with pytest.raises(PathEscapeError, match="Path escape detected"):
            workspace.resolve_path("../../etc/passwd")

    def test_path_escape_with_absolute_rejected(
        self, workspace: WorkspaceAccessor,
    ) -> None:
        with pytest.raises(PathEscapeError):
            workspace.resolve_path("../../../etc/passwd")

    def test_nested_dotdot_escape_rejected(
        self, workspace: WorkspaceAccessor,
    ) -> None:
        with pytest.raises(PathEscapeError):
            workspace.resolve_path("uploads/../../..")

    def test_valid_nested_path(self, workspace: WorkspaceAccessor) -> None:
        resolved = workspace.resolve_path("outputs/sub/file.txt")
        assert str(resolved).endswith("outputs/sub/file.txt")


class TestWritePermission:
    """Tests for check_write_permission - writable zone enforcement."""

    def test_outputs_is_writable(self, workspace: WorkspaceAccessor) -> None:
        resolved = workspace.resolve_path("outputs/result.txt")
        assert workspace.check_write_permission(resolved) is True

    def test_scratch_is_writable(self, workspace: WorkspaceAccessor) -> None:
        resolved = workspace.resolve_path("scratch/temp.txt")
        assert workspace.check_write_permission(resolved) is True

    def test_uploads_not_writable(self, workspace: WorkspaceAccessor) -> None:
        resolved = workspace.resolve_path("uploads/file.txt")
        assert workspace.check_write_permission(resolved) is False

    def test_refs_not_writable(self, workspace: WorkspaceAccessor) -> None:
        resolved = workspace.resolve_path("refs/shared.txt")
        assert workspace.check_write_permission(resolved) is False

    def test_root_not_writable(self, workspace: WorkspaceAccessor) -> None:
        resolved = workspace.resolve_path("some_file.txt")
        assert workspace.check_write_permission(resolved) is False


class TestReadFile:
    """Tests for read_file."""

    def test_read_existing_file(self, workspace: WorkspaceAccessor) -> None:
        content = workspace.read_file("uploads/file.txt")
        assert content == b"upload content"

    def test_read_nonexistent_file_raises(
        self, workspace: WorkspaceAccessor,
    ) -> None:
        with pytest.raises(FileNotFoundError):
            workspace.read_file("uploads/missing.txt")

    def test_read_escape_rejected(self, workspace: WorkspaceAccessor) -> None:
        with pytest.raises(PathEscapeError):
            workspace.read_file("../../etc/passwd")


class TestWriteFile:
    """Tests for write_file."""

    def test_write_to_outputs(self, workspace: WorkspaceAccessor) -> None:
        path = workspace.write_file("outputs/result.txt", b"result data")
        assert path.exists()
        assert path.read_bytes() == b"result data"

    def test_write_to_scratch(self, workspace: WorkspaceAccessor) -> None:
        path = workspace.write_file("scratch/temp.bin", b"\x00\x01")
        assert path.exists()

    def test_write_to_uploads_denied(
        self, workspace: WorkspaceAccessor,
    ) -> None:
        with pytest.raises(WritePermissionError, match="Write denied"):
            workspace.write_file("uploads/hack.txt", b"bad")

    def test_write_creates_subdirectories(
        self, workspace: WorkspaceAccessor,
    ) -> None:
        path = workspace.write_file("outputs/deep/nested/file.txt", b"deep")
        assert path.exists()
        assert path.read_bytes() == b"deep"

    def test_write_escape_rejected(
        self, workspace: WorkspaceAccessor,
    ) -> None:
        with pytest.raises(PathEscapeError):
            workspace.write_file("../../etc/evil", b"bad")
