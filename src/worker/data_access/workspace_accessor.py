"""
WorkspaceAccessor - sandbox file access within data/ directory.

Enforces:
- Path escape prevention (no traversal outside data root)
- Write permission zones: outputs/ and scratch/ are writable, rest read-only
"""
from pathlib import Path

from src.worker.data_access.models import DataSpaceConfig


_WRITABLE_DIRS = ("outputs", "scratch")


class PathEscapeError(Exception):
    """Raised when a path attempts to escape the sandbox."""


class WritePermissionError(Exception):
    """Raised when writing to a read-only zone."""


class WorkspaceAccessor:
    """Path sandbox: restricts file access within data/, with write zones."""

    def __init__(self, worker_base: Path, config: DataSpaceConfig) -> None:
        self._data_root = (worker_base / config.root).resolve()
        self._config = config

    @property
    def data_root(self) -> Path:
        return self._data_root

    def resolve_path(self, relative_path: str) -> Path:
        """Resolve to absolute path, rejecting escapes outside data_root."""
        resolved = (self._data_root / relative_path).resolve()
        if not _is_within(resolved, self._data_root):
            raise PathEscapeError(
                f"Path escape detected: '{relative_path}' resolves outside data root"
            )
        return resolved

    def check_write_permission(self, resolved_path: Path) -> bool:
        """Return True if resolved_path is in a writable zone (outputs/ or scratch/)."""
        try:
            rel = resolved_path.relative_to(self._data_root)
        except ValueError:
            return False
        top_dir = rel.parts[0] if rel.parts else ""
        return top_dir in _WRITABLE_DIRS

    def read_file(self, relative_path: str) -> bytes:
        """Resolve path and read file contents."""
        resolved = self.resolve_path(relative_path)
        if not resolved.exists():
            raise FileNotFoundError(f"File not found: {relative_path}")
        return resolved.read_bytes()

    def write_file(self, relative_path: str, content: bytes) -> Path:
        """Resolve path, check write permission, then write."""
        resolved = self.resolve_path(relative_path)
        if not self.check_write_permission(resolved):
            raise WritePermissionError(
                f"Write denied: '{relative_path}' is not in a writable zone"
            )
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(content)
        return resolved


def _is_within(path: Path, parent: Path) -> bool:
    """Check if path is within parent directory."""
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
