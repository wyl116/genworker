"""File-backed lifecycle feedback storage."""
from __future__ import annotations

import json
from pathlib import Path

from src.common.logger import get_logger

from .models import FeedbackRecord

logger = get_logger()


class FeedbackStore:
    """Append-only feedback storage under a worker lifecycle directory."""

    def __init__(self, workspace_root: Path | str) -> None:
        self._workspace_root = Path(workspace_root)

    def append(self, tenant_id: str, worker_id: str, record: FeedbackRecord) -> None:
        """Append one feedback record."""
        file_path = self._file_path(tenant_id, worker_id)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def list_all(self, tenant_id: str, worker_id: str) -> tuple[FeedbackRecord, ...]:
        """Read all feedback records for one worker."""
        file_path = self._file_path(tenant_id, worker_id)
        if not file_path.is_file():
            return ()
        records: list[FeedbackRecord] = []
        for raw in file_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning("[FeedbackStore] Invalid feedback row in %s: %s", file_path, exc)
                continue
            records.append(FeedbackRecord.from_dict(data))
        return tuple(records)

    def list_for_target(
        self,
        tenant_id: str,
        worker_id: str,
        *,
        target_type: str,
        target_id: str,
    ) -> tuple[FeedbackRecord, ...]:
        """Return feedback for a specific target."""
        return tuple(
            record
            for record in self.list_all(tenant_id, worker_id)
            if record.target_type == target_type and record.target_id == target_id
        )

    def _file_path(self, tenant_id: str, worker_id: str) -> Path:
        return (
            self._workspace_root / "tenants" / tenant_id / "workers" / worker_id
            / "lifecycle" / "feedback" / "feedback.jsonl"
        )
