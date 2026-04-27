"""Session inbox storage with Redis-or-file fallback."""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from src.common.logger import get_logger
from src.common.runtime_status import ComponentRuntimeStatus, ComponentStatus
from src.common.time import utc_now_iso
from src.events.models import Event, EventBusProtocol

logger = get_logger()


class InboxStatus(str, Enum):
    """Inbox item lifecycle."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    CONSUMED = "CONSUMED"


@dataclass(frozen=True)
class InboxItem:
    """Structured fact written by the sensing layer."""

    tenant_id: str
    worker_id: str
    source_type: str
    event_type: str
    inbox_id: str = field(default_factory=lambda: uuid4().hex)
    target_session_key: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    priority_hint: int = 0
    dedupe_key: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = InboxStatus.PENDING.value
    processing_at: str = ""
    consumed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InboxItem":
        return cls(
            inbox_id=str(data.get("inbox_id", "") or uuid4().hex),
            tenant_id=str(data.get("tenant_id", "")),
            worker_id=str(data.get("worker_id", "")),
            target_session_key=str(data.get("target_session_key", "")),
            source_type=str(data.get("source_type", "")),
            event_type=str(data.get("event_type", "")),
            created_at=str(data.get("created_at", "") or utc_now_iso()),
            priority_hint=int(data.get("priority_hint", 0) or 0),
            dedupe_key=str(data.get("dedupe_key", "")),
            payload=dict(data.get("payload", {})),
            status=str(data.get("status", InboxStatus.PENDING.value)),
            processing_at=str(data.get("processing_at", "")),
            consumed_at=str(data.get("consumed_at", "")),
        )


class SessionInboxStore:
    """Store inbox items with simple atomic fetch semantics."""

    def __init__(
        self,
        redis_client: Any | None = None,
        fallback_dir: Path | str = "workspace",
        event_bus: EventBusProtocol | None = None,
        processing_timeout_minutes: int = 10,
    ) -> None:
        self._redis = redis_client
        self._fallback_dir = Path(fallback_dir)
        self._event_bus = event_bus
        self._processing_timeout = timedelta(
            minutes=max(processing_timeout_minutes, 1)
        )
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._status = ComponentStatus.READY
        self._last_error = ""
        self._selected_backend = "redis" if redis_client is not None else "file"

    def runtime_status(self) -> ComponentRuntimeStatus:
        return ComponentRuntimeStatus(
            component="inbox_store",
            enabled=True,
            status=self._status,
            selected_backend=self._selected_backend,
            primary_backend="redis",
            fallback_backend="file",
            ground_truth="file",
            last_error=self._last_error,
        )

    async def write(self, item: InboxItem) -> InboxItem:
        normalized = item if item.inbox_id else InboxItem(**item.to_dict())
        await self._upsert(normalized)
        await self._publish_inbox_written(normalized)
        return normalized

    async def fetch_pending(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        limit: int = 50,
        exclude_event_types: Sequence[str] = (),
    ) -> tuple[InboxItem, ...]:
        async with self._lock_for(tenant_id, worker_id):
            items = await self._load_worker_items(tenant_id, worker_id)
            items = self._rollback_stale_items(items)
            excluded = {
                str(event_type).strip()
                for event_type in exclude_event_types
                if str(event_type).strip()
            }
            pending = sorted(
                (
                    item for item in items.values()
                    if item.status == InboxStatus.PENDING.value
                    and item.event_type not in excluded
                ),
                key=lambda item: (-item.priority_hint, item.created_at, item.inbox_id),
            )
            selected = pending[:max(limit, 0)]
            if not selected:
                if items:
                    await self._persist_worker_items(tenant_id, worker_id, items)
                return ()

            processing_at = utc_now_iso()
            updated: list[InboxItem] = []
            for item in selected:
                next_item = InboxItem.from_dict(
                    {
                        **item.to_dict(),
                        "status": InboxStatus.PROCESSING.value,
                        "processing_at": processing_at,
                    }
                )
                items[next_item.inbox_id] = next_item
                updated.append(next_item)
            await self._persist_worker_items(tenant_id, worker_id, items)
            return tuple(updated)

    async def list_pending(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        limit: int = 50,
        event_type: str = "",
        source_type: str = "",
    ) -> tuple[InboxItem, ...]:
        async with self._lock_for(tenant_id, worker_id):
            original_items = await self._load_worker_items(tenant_id, worker_id)
            items = self._rollback_stale_items(original_items)
            if items != original_items:
                await self._persist_worker_items(tenant_id, worker_id, items)
            wanted_event = str(event_type).strip()
            wanted_source = str(source_type).strip()
            pending = sorted(
                (
                    item for item in items.values()
                    if item.status == InboxStatus.PENDING.value
                    and (not wanted_event or item.event_type == wanted_event)
                    and (not wanted_source or item.source_type == wanted_source)
                ),
                key=lambda item: (-item.priority_hint, item.created_at, item.inbox_id),
            )
            return tuple(pending[:max(limit, 0)])

    async def claim_pending(
        self,
        inbox_id: str,
        *,
        tenant_id: str,
        worker_id: str,
        event_type: str = "",
    ) -> InboxItem | None:
        """Atomically claim one pending inbox item for command processing."""
        wanted_event = str(event_type).strip()
        async with self._lock_for(tenant_id, worker_id):
            items = await self._load_worker_items(tenant_id, worker_id)
            items = self._rollback_stale_items(items)
            item = items.get(str(inbox_id).strip())
            if (
                item is None
                or item.status != InboxStatus.PENDING.value
                or (wanted_event and item.event_type != wanted_event)
            ):
                if items:
                    await self._persist_worker_items(tenant_id, worker_id, items)
                return None
            claimed = InboxItem.from_dict(
                {
                    **item.to_dict(),
                    "status": InboxStatus.PROCESSING.value,
                    "processing_at": utc_now_iso(),
                }
            )
            items[claimed.inbox_id] = claimed
            await self._persist_worker_items(tenant_id, worker_id, items)
            return claimed

    async def mark_consumed(
        self,
        inbox_ids: list[str] | tuple[str, ...],
        *,
        tenant_id: str = "",
        worker_id: str = "",
    ) -> None:
        await self._update_status(
            inbox_ids=inbox_ids,
            tenant_id=tenant_id,
            worker_id=worker_id,
            status=InboxStatus.CONSUMED.value,
            timestamp_field="consumed_at",
        )

    async def requeue_processing(
        self,
        inbox_ids: list[str] | tuple[str, ...],
        *,
        tenant_id: str = "",
        worker_id: str = "",
    ) -> None:
        await self._update_status(
            inbox_ids=inbox_ids,
            tenant_id=tenant_id,
            worker_id=worker_id,
            status=InboxStatus.PENDING.value,
            timestamp_field="processing_at",
            clear_timestamp=True,
        )

    async def mark_error(
        self,
        inbox_id: str,
        *,
        reason: str,
        tenant_id: str = "",
        worker_id: str = "",
    ) -> None:
        """Return an inbox item to pending and annotate the last failure reason."""
        target = await self.get_by_id(
            inbox_id,
            tenant_id=tenant_id,
            worker_id=worker_id,
        )
        if target is None:
            return
        async with self._lock_for(target.tenant_id, target.worker_id):
            items = await self._load_worker_items(target.tenant_id, target.worker_id)
            current = items.get(inbox_id)
            if current is None:
                return
            payload = dict(current.payload)
            payload["last_error"] = str(reason)
            payload["last_error_at"] = utc_now_iso()
            items[inbox_id] = InboxItem.from_dict(
                {
                    **current.to_dict(),
                    "payload": payload,
                    "status": InboxStatus.PENDING.value,
                    "processing_at": "",
                }
            )
            await self._persist_worker_items(target.tenant_id, target.worker_id, items)

    async def get_by_id(
        self,
        inbox_id: str,
        *,
        tenant_id: str = "",
        worker_id: str = "",
    ) -> InboxItem | None:
        locations = (
            [(tenant_id, worker_id)]
            if tenant_id and worker_id
            else await self._iter_locations()
        )
        for current_tenant, current_worker in locations:
            items = await self._load_worker_items(current_tenant, current_worker)
            found = items.get(inbox_id)
            if found is not None:
                return found
        return None

    async def _update_status(
        self,
        *,
        inbox_ids: list[str] | tuple[str, ...],
        tenant_id: str,
        worker_id: str,
        status: str,
        timestamp_field: str,
        clear_timestamp: bool = False,
    ) -> None:
        inbox_id_set = {str(inbox_id) for inbox_id in inbox_ids}
        if not inbox_id_set:
            return

        locations = (
            [(tenant_id, worker_id)]
            if tenant_id and worker_id
            else await self._iter_locations()
        )
        for current_tenant, current_worker in locations:
            async with self._lock_for(current_tenant, current_worker):
                items = await self._load_worker_items(current_tenant, current_worker)
                changed = False
                for inbox_id in inbox_id_set:
                    existing = items.get(inbox_id)
                    if existing is None:
                        continue
                    payload = existing.to_dict()
                    payload["status"] = status
                    payload[timestamp_field] = "" if clear_timestamp else utc_now_iso()
                    if clear_timestamp and timestamp_field != "consumed_at":
                        payload["consumed_at"] = existing.consumed_at
                    items[inbox_id] = InboxItem.from_dict(payload)
                    changed = True
                if changed:
                    await self._persist_worker_items(
                        current_tenant, current_worker, items
                    )

    async def _upsert(self, item: InboxItem) -> None:
        async with self._lock_for(item.tenant_id, item.worker_id):
            items = await self._load_worker_items(item.tenant_id, item.worker_id)
            items[item.inbox_id] = item
            await self._persist_worker_items(item.tenant_id, item.worker_id, items)

    async def _load_worker_items(
        self,
        tenant_id: str,
        worker_id: str,
    ) -> dict[str, InboxItem]:
        if self._redis is not None:
            try:
                raw = await self._redis.hgetall(self._redis_key(tenant_id, worker_id))
                if raw:
                    return {
                        str(inbox_id): InboxItem.from_dict(json.loads(serialized))
                        for inbox_id, serialized in raw.items()
                    }
            except Exception as exc:
                self._mark_redis_fallback(exc, operation="read")

        file_path = self._file_path(tenant_id, worker_id)
        if not file_path.is_file():
            return {}
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[SessionInboxStore] File read failed: %s", exc)
            return {}
        return {
            str(inbox_id): InboxItem.from_dict(data)
            for inbox_id, data in payload.items()
        }

    async def _persist_worker_items(
        self,
        tenant_id: str,
        worker_id: str,
        items: dict[str, InboxItem],
    ) -> None:
        if self._redis is not None:
            try:
                if items:
                    await self._redis.hset(
                        self._redis_key(tenant_id, worker_id),
                        mapping={
                            inbox_id: json.dumps(item.to_dict(), ensure_ascii=False)
                            for inbox_id, item in items.items()
                        },
                    )
                else:
                    await self._redis.delete(self._redis_key(tenant_id, worker_id))
            except Exception as exc:
                self._mark_redis_fallback(exc, operation="write")

        file_path = self._file_path(tenant_id, worker_id)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            json.dumps(
                {
                    inbox_id: item.to_dict()
                    for inbox_id, item in items.items()
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _rollback_stale_items(
        self,
        items: dict[str, InboxItem],
    ) -> dict[str, InboxItem]:
        current = utc_now_iso()
        updated: dict[str, InboxItem] = {}
        for inbox_id, item in items.items():
            if (
                item.status == InboxStatus.PROCESSING.value
                and item.processing_at
                and self._is_stale(item.processing_at, current)
            ):
                updated[inbox_id] = InboxItem.from_dict(
                    {
                        **item.to_dict(),
                        "status": InboxStatus.PENDING.value,
                        "processing_at": "",
                    }
                )
                continue
            updated[inbox_id] = item
        return updated

    def _is_stale(self, processing_at: str, current: str) -> bool:
        from datetime import datetime, timezone

        try:
            started = datetime.fromisoformat(processing_at)
            now = datetime.fromisoformat(current)
        except ValueError:
            return False
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return (now - started) >= self._processing_timeout

    async def _publish_inbox_written(self, item: InboxItem) -> None:
        if self._event_bus is None:
            return
        await self._event_bus.publish(Event(
            event_id=f"evt-{uuid4().hex[:8]}",
            type="inbox.item_written",
            source="session_inbox_store",
            tenant_id=item.tenant_id,
            payload=(
                ("tenant_id", item.tenant_id),
                ("worker_id", item.worker_id),
                ("inbox_id", item.inbox_id),
                ("event_type", item.event_type),
                ("source_type", item.source_type),
            ),
        ))

    async def _iter_locations(self) -> list[tuple[str, str]]:
        locations: set[tuple[str, str]] = set()
        if self._redis is not None:
            try:
                keys = await self._redis.keys("inbox:*")
                for key in keys:
                    parts = str(key).split(":")
                    if len(parts) >= 3:
                        locations.add((parts[1], parts[2]))
            except Exception as exc:
                self._mark_redis_fallback(exc, operation="scan")

        inbox_root = self._fallback_dir / "tenants"
        if inbox_root.is_dir():
            for tenant_dir in inbox_root.iterdir():
                worker_root = tenant_dir / "workers"
                if not worker_root.is_dir():
                    continue
                for worker_dir in worker_root.iterdir():
                    inbox_file = worker_dir / "runtime" / "inbox.json"
                    if inbox_file.is_file():
                        locations.add((tenant_dir.name, worker_dir.name))
        return sorted(locations)

    def _lock_for(self, tenant_id: str, worker_id: str) -> asyncio.Lock:
        key = (tenant_id, worker_id)
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def _redis_key(self, tenant_id: str, worker_id: str) -> str:
        return f"inbox:{tenant_id}:{worker_id}"

    def _file_path(self, tenant_id: str, worker_id: str) -> Path:
        return (
            self._fallback_dir
            / "tenants"
            / tenant_id
            / "workers"
            / worker_id
            / "runtime"
            / "inbox.json"
        )

    def _mark_redis_fallback(self, exc: Exception, *, operation: str) -> None:
        if self._redis is None:
            return
        self._status = ComponentStatus.DEGRADED
        self._selected_backend = "file"
        self._last_error = str(exc).splitlines()[0][:200]
        logger.warning(
            "[SessionInboxStore] Redis %s failed, fallback to file: %s",
            operation,
            self._last_error,
        )
