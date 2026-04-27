"""Workspace file polling sensor."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..base import SensorBase
from ..protocol import SensedFact


class WorkspaceFileSensor(SensorBase):
    """Scan configured directories and report new or modified files."""

    def __init__(
        self,
        *,
        watch_paths: tuple[str, ...],
        patterns: tuple[str, ...] = ("*",),
    ) -> None:
        super().__init__(fallback_route="heartbeat")
        self._watch_paths = watch_paths
        self._patterns = patterns or ("*",)
        self._snapshot: dict[str, dict[str, Any]] = {}

    @property
    def sensor_type(self) -> str:
        return "workspace_file"

    @property
    def delivery_mode(self) -> str:
        return "poll"

    async def poll(self) -> tuple[SensedFact, ...]:
        current = self._scan_directories()
        changed = self._diff_snapshot(current)
        self._snapshot = current
        return tuple(
            SensedFact(
                source_type="workspace_file",
                event_type="local.file_changed",
                dedupe_key=f"file:{path}:{info['mtime']}",
                payload=(
                    ("path", path),
                    ("size", info["size"]),
                    ("mtime", info["mtime"]),
                ),
                priority_hint=10,
                cognition_route="heartbeat",
            )
            for path, info in changed.items()
        )

    def get_snapshot(self) -> dict[str, Any]:
        return {"file_snapshot": self._snapshot}

    def restore_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = dict(snapshot.get("file_snapshot", {}))

    def _scan_directories(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for watch_path in self._watch_paths:
            root = Path(watch_path)
            if not root.exists():
                continue
            for pattern in self._patterns:
                for file_path in root.glob(pattern):
                    if not file_path.is_file():
                        continue
                    stat = file_path.stat()
                    result[str(file_path)] = {
                        "mtime": stat.st_mtime,
                        "size": stat.st_size,
                    }
        return result

    def _diff_snapshot(
        self,
        current: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        changed: dict[str, dict[str, Any]] = {}
        previous = self._snapshot
        for path, info in current.items():
            old = previous.get(path)
            if old is None or old != info:
                changed[path] = info
        return changed
