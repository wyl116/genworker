"""Project-rooted path helpers."""
from __future__ import annotations

from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def project_root() -> Path:
    """Return the repository root."""
    return _PROJECT_ROOT


def resolve_project_path(path: str | Path) -> Path:
    """Resolve a path relative to the repository root unless already absolute."""
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return _PROJECT_ROOT / candidate


def default_workspace_root() -> Path:
    """Return the default workspace directory under the repository root."""
    return _PROJECT_ROOT / "workspace"


def resolve_workspace_root(path: str | Path | None = None) -> Path:
    """Resolve an explicit or default workspace root against the repository root."""
    if path in (None, ""):
        return default_workspace_root()
    return resolve_project_path(path)
