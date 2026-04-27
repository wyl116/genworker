"""Attention ledger for heartbeat dedupe."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.common.logger import get_logger
from src.common.runtime_status import ComponentRuntimeStatus, ComponentStatus

logger = get_logger()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AttentionLedger:
    """Record recently surfaced dedupe keys."""

    def __init__(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        redis_client: Any | None = None,
        workspace_root: Path | str = "workspace",
        ttl_hours: int = 48,
    ) -> None:
        self._tenant_id = tenant_id
        self._worker_id = worker_id
        self._redis = redis_client
        self._workspace_root = Path(workspace_root)
        self._ttl_hours = max(ttl_hours, 1)
        self._status = ComponentStatus.READY
        self._last_error = ""
        self._selected_backend = "redis" if redis_client is not None else "file"

    def runtime_status(self) -> ComponentRuntimeStatus:
        return ComponentRuntimeStatus(
            component="attention_ledger",
            enabled=True,
            status=self._status,
            selected_backend=self._selected_backend,
            primary_backend="redis",
            fallback_backend="file",
            ground_truth="file",
            last_error=self._last_error,
        )

    async def has_notified(
        self,
        dedupe_key: str,
        window_hours: int = 24,
    ) -> bool:
        if not dedupe_key:
            return False
        record = await self._load_record(dedupe_key)
        if not record:
            return False
        timestamp = record.get("notified_at", "")
        if not timestamp:
            return False
        try:
            notified_at = datetime.fromisoformat(timestamp)
        except ValueError:
            return False
        if notified_at.tzinfo is None:
            notified_at = notified_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - notified_at <= timedelta(
            hours=max(window_hours, 1)
        )

    async def record_notification(self, dedupe_key: str, summary: str) -> None:
        if not dedupe_key:
            return
        payload = {"summary": summary, "notified_at": _now_iso()}
        if self._redis is not None:
            try:
                await self._redis.hset(
                    self._redis_key(),
                    field=dedupe_key,
                    value=json.dumps(payload, ensure_ascii=False),
                )
                await self._redis.expire(self._redis_key(), self._ttl_hours * 3600)
                return
            except Exception as exc:
                self._mark_fallback(exc)
                logger.warning("[AttentionLedger] Redis write failed: %s", exc)
        records = self._load_file_records()
        records[dedupe_key] = payload
        self._save_file_records(records)

    async def _load_record(self, dedupe_key: str) -> dict[str, str] | None:
        if self._redis is not None:
            try:
                raw = await self._redis.hget(self._redis_key(), dedupe_key)
                if raw:
                    return json.loads(raw)
            except Exception as exc:
                self._mark_fallback(exc)
                logger.warning("[AttentionLedger] Redis read failed: %s", exc)
        return self._load_file_records().get(dedupe_key)

    def _mark_fallback(self, exc: Exception) -> None:
        self._status = ComponentStatus.DEGRADED
        self._selected_backend = "file"
        self._last_error = str(exc).splitlines()[0][:200]

    def _load_file_records(self) -> dict[str, dict[str, str]]:
        file_path = self._file_path()
        if not file_path.is_file():
            return {}
        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[AttentionLedger] Failed to read %s: %s", file_path, exc)
            return {}

    def _save_file_records(self, records: dict[str, dict[str, str]]) -> None:
        file_path = self._file_path()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _redis_key(self) -> str:
        return f"attention:{self._tenant_id}:{self._worker_id}"

    def _file_path(self) -> Path:
        return (
            self._workspace_root
            / "tenants"
            / self._tenant_id
            / "workers"
            / self._worker_id
            / "attention_ledger.json"
        )
