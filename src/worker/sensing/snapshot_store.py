"""Persistent storage for sensor snapshots."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SnapshotStore:
    """Persist one worker's sensor snapshots to disk."""

    def __init__(self, workspace_root: Path, tenant_id: str, worker_id: str) -> None:
        self._dir = (
            Path(workspace_root)
            / "tenants"
            / tenant_id
            / "workers"
            / worker_id
            / "sensor_snapshots"
        )

    async def save(self, sensor_type: str, snapshot: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{sensor_type}.json"
        path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def load(self, sensor_type: str) -> dict[str, Any]:
        path = self._dir / f"{sensor_type}.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
