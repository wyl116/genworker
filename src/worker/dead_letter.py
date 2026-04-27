"""Dead-letter storage for exhausted scheduled tasks."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.common.logger import get_logger
from src.common.runtime_status import ComponentRuntimeStatus, ComponentStatus

logger = get_logger()


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple((str(key), _freeze(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, tuple):
        if all(isinstance(item, tuple) and len(item) == 2 for item in value):
            return {str(key): _thaw(item) for key, item in value}
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class DeadLetterEntry:
    """One permanently failed scheduled task."""

    entry_id: str
    worker_id: str
    tenant_id: str
    task_description: str
    error_message: str
    retry_count: int
    failed_at: str
    job_snapshot: tuple[tuple[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "worker_id": self.worker_id,
            "tenant_id": self.tenant_id,
            "task_description": self.task_description,
            "error_message": self.error_message,
            "retry_count": self.retry_count,
            "failed_at": self.failed_at,
            "job_snapshot": _thaw(self.job_snapshot),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeadLetterEntry":
        snapshot = data.get("job_snapshot", {})
        frozen_snapshot = _freeze(snapshot)
        if isinstance(frozen_snapshot, tuple) and all(
            isinstance(item, tuple) and len(item) == 2 for item in frozen_snapshot
        ):
            job_snapshot = frozen_snapshot
        else:
            job_snapshot = (("value", frozen_snapshot),)
        return cls(
            entry_id=str(data.get("entry_id", "")),
            worker_id=str(data.get("worker_id", "")),
            tenant_id=str(data.get("tenant_id", "")),
            task_description=str(data.get("task_description", "")),
            error_message=str(data.get("error_message", "")),
            retry_count=int(data.get("retry_count", 0) or 0),
            failed_at=str(data.get("failed_at", "")),
            job_snapshot=job_snapshot,
        )


class DeadLetterStore:
    """Redis-primary dead-letter storage with file fallback."""

    _REDIS_PREFIX = "lw:dead_letter"

    def __init__(
        self,
        redis_client: Any | None = None,
        fallback_dir: Path | str = "workspace",
    ) -> None:
        self._redis = redis_client
        self._fallback_dir = Path(fallback_dir)
        self._status = ComponentStatus.READY
        self._last_error = ""
        self._selected_backend = "redis" if redis_client is not None else "file"

    def runtime_status(self) -> ComponentRuntimeStatus:
        return ComponentRuntimeStatus(
            component="dead_letter_store",
            enabled=True,
            status=self._status,
            selected_backend=self._selected_backend,
            primary_backend="redis",
            fallback_backend="file",
            ground_truth="file",
            last_error=self._last_error,
        )

    async def add(self, entry: DeadLetterEntry) -> None:
        if self._redis is not None:
            try:
                await self._redis.hset(
                    self._redis_key(entry.worker_id),
                    field=entry.entry_id,
                    value=json.dumps(entry.to_dict(), ensure_ascii=False),
                )
                return
            except Exception as exc:
                self._mark_fallback(exc)
                logger.warning("[DeadLetterStore] Redis add failed: %s", exc)
        self._write_file(entry)

    async def list_entries(self, worker_id: str) -> tuple[DeadLetterEntry, ...]:
        if self._redis is not None:
            try:
                data = await self._redis.hgetall(self._redis_key(worker_id))
                return tuple(
                    DeadLetterEntry.from_dict(json.loads(raw))
                    for _, raw in sorted(data.items())
                )
            except Exception as exc:
                self._mark_fallback(exc)
                logger.warning("[DeadLetterStore] Redis list failed: %s", exc)
        return self._list_files(worker_id)

    async def discard(self, worker_id: str, entry_id: str) -> bool:
        if self._redis is not None:
            try:
                removed = await self._redis.hdel(self._redis_key(worker_id), entry_id)
                return bool(removed)
            except Exception as exc:
                self._mark_fallback(exc)
                logger.warning("[DeadLetterStore] Redis discard failed: %s", exc)
        return self._delete_file(worker_id, entry_id)

    async def retry(
        self,
        worker_id: str,
        entry_id: str,
    ) -> DeadLetterEntry | None:
        if self._redis is not None:
            try:
                raw = await self._redis.hget(self._redis_key(worker_id), entry_id)
                if raw is None:
                    return None
                await self._redis.hdel(self._redis_key(worker_id), entry_id)
                return DeadLetterEntry.from_dict(json.loads(raw))
            except Exception as exc:
                self._mark_fallback(exc)
                logger.warning("[DeadLetterStore] Redis retry failed: %s", exc)
        entry = self._read_file(worker_id, entry_id)
        if entry is None:
            return None
        self._delete_file(worker_id, entry_id)
        return entry

    def _mark_fallback(self, exc: Exception) -> None:
        self._status = ComponentStatus.DEGRADED
        self._selected_backend = "file"
        self._last_error = str(exc).splitlines()[0][:200]

    def _redis_key(self, worker_id: str) -> str:
        return f"{self._REDIS_PREFIX}:{worker_id}"

    def _write_file(self, entry: DeadLetterEntry) -> None:
        path = self._file_path(entry.tenant_id, entry.worker_id, entry.entry_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(entry.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _list_files(self, worker_id: str) -> tuple[DeadLetterEntry, ...]:
        entries: list[DeadLetterEntry] = []
        pattern = f"tenants/*/workers/{worker_id}/dead_letter/*.json"
        for path in sorted(self._fallback_dir.glob(pattern)):
            entry = self._read_path(path)
            if entry is not None:
                entries.append(entry)
        return tuple(entries)

    def _read_file(
        self,
        worker_id: str,
        entry_id: str,
    ) -> DeadLetterEntry | None:
        pattern = f"tenants/*/workers/{worker_id}/dead_letter/{entry_id}.json"
        for path in self._fallback_dir.glob(pattern):
            return self._read_path(path)
        return None

    def _read_path(self, path: Path) -> DeadLetterEntry | None:
        try:
            return DeadLetterEntry.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("[DeadLetterStore] Failed to read %s: %s", path, exc)
            return None

    def _delete_file(self, worker_id: str, entry_id: str) -> bool:
        pattern = f"tenants/*/workers/{worker_id}/dead_letter/{entry_id}.json"
        deleted = False
        for path in self._fallback_dir.glob(pattern):
            try:
                path.unlink(missing_ok=True)
                deleted = True
            except OSError as exc:
                logger.warning("[DeadLetterStore] Failed to delete %s: %s", path, exc)
        return deleted

    def _file_path(self, tenant_id: str, worker_id: str, entry_id: str) -> Path:
        return (
            self._fallback_dir
            / "tenants"
            / tenant_id
            / "workers"
            / worker_id
            / "dead_letter"
            / f"{entry_id}.json"
        )
