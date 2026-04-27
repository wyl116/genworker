"""File-backed lifecycle suggestion storage."""
from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from src.common.logger import get_logger

from .models import SuggestionRecord, now_iso, parse_iso

logger = get_logger()


class SuggestionStore:
    """Persist pending and resolved lifecycle suggestions under a worker."""

    _CLAIM_TIMEOUT_SECONDS = 600
    _CLAIM_HEARTBEAT_SECONDS = 30

    def __init__(self, workspace_root: Path | str) -> None:
        self._workspace_root = Path(workspace_root)

    def create(
        self,
        tenant_id: str,
        worker_id: str,
        record: SuggestionRecord,
        *,
        cooldown_days: int = 14,
    ) -> SuggestionRecord | None:
        """Create a pending suggestion when lifecycle state permits it."""
        if self.creation_block_reason(
            tenant_id=tenant_id,
            worker_id=worker_id,
            suggestion_type=record.type,
            source_entity_id=record.source_entity_id,
            cooldown_days=cooldown_days,
        ) is not None:
            return None
        self.save_pending(tenant_id, worker_id, record)
        return record

    def creation_block_reason(
        self,
        tenant_id: str,
        worker_id: str,
        *,
        suggestion_type: str,
        source_entity_id: str,
        cooldown_days: int = 14,
    ) -> str | None:
        """Return the canonical reason a suggestion source cannot be re-created."""
        self._recover_stale_claims(tenant_id, worker_id)
        self.expire_pending(tenant_id, worker_id)
        if self.find_pending(
            tenant_id=tenant_id,
            worker_id=worker_id,
            suggestion_type=suggestion_type,
            source_entity_id=source_entity_id,
        ) is not None:
            return "duplicate pending suggestion"
        if self.find_resolved(
            tenant_id=tenant_id,
            worker_id=worker_id,
            suggestion_type=suggestion_type,
            source_entity_id=source_entity_id,
            statuses=("approved",),
        ) is not None:
            return "already approved"
        if self.was_rejected_recently(
            tenant_id=tenant_id,
            worker_id=worker_id,
            suggestion_type=suggestion_type,
            source_entity_id=source_entity_id,
            cooldown_days=cooldown_days,
        ):
            return "recently rejected"
        return None

    def save_pending(self, tenant_id: str, worker_id: str, record: SuggestionRecord) -> None:
        """Persist a pending suggestion."""
        self._pending_dir(tenant_id, worker_id).mkdir(parents=True, exist_ok=True)
        self._write_record(
            self._pending_file(tenant_id, worker_id, record.suggestion_id),
            record,
        )

    def get(
        self,
        tenant_id: str,
        worker_id: str,
        suggestion_id: str,
    ) -> SuggestionRecord | None:
        """Load one suggestion from pending or resolved storage."""
        for file_path in (
            self._pending_file(tenant_id, worker_id, suggestion_id),
            self._resolved_file(tenant_id, worker_id, suggestion_id),
        ):
            if file_path.is_file():
                return self._read_record(file_path)
        return None

    def get_any(
        self,
        tenant_id: str,
        worker_id: str,
        suggestion_id: str,
    ) -> SuggestionRecord | None:
        """Load one suggestion from pending, claimed, or resolved storage."""
        self._recover_stale_claims(tenant_id, worker_id)
        self.expire_pending(tenant_id, worker_id)
        return self._read_claimed_pending_or_resolved(tenant_id, worker_id, suggestion_id)

    def get_state(
        self,
        tenant_id: str,
        worker_id: str,
        suggestion_id: str,
    ) -> str:
        """Return the current lifecycle state of one suggestion id."""
        self._recover_stale_claims(tenant_id, worker_id)
        self.expire_pending(tenant_id, worker_id)
        pending_file = self._pending_file(tenant_id, worker_id, suggestion_id)
        if pending_file.is_file():
            return "pending"
        claimed_file = self._claimed_file(tenant_id, worker_id, suggestion_id)
        if claimed_file.is_file():
            return "claimed"
        resolved = self._read_record(self._resolved_file(tenant_id, worker_id, suggestion_id))
        if resolved is not None:
            return resolved.status or "resolved"
        return "missing"

    def get_pending_active(
        self,
        tenant_id: str,
        worker_id: str,
        suggestion_id: str,
    ) -> SuggestionRecord | None:
        """Load one pending suggestion after expiring stale records."""
        self._recover_stale_claims(tenant_id, worker_id)
        self.expire_pending(tenant_id, worker_id)
        file_path = self._pending_file(tenant_id, worker_id, suggestion_id)
        if not file_path.is_file():
            return None
        record = self._read_record(file_path)
        if record is None or record.status != "pending":
            return None
        return record

    def claim_pending(
        self,
        tenant_id: str,
        worker_id: str,
        suggestion_id: str,
    ) -> SuggestionRecord | None:
        """Atomically claim one pending suggestion for approval processing."""
        self._recover_stale_claims(tenant_id, worker_id)
        self.expire_pending(tenant_id, worker_id)
        pending_file = self._pending_file(tenant_id, worker_id, suggestion_id)
        claimed_file = self._claimed_file(tenant_id, worker_id, suggestion_id)
        if not pending_file.is_file():
            return None
        self._claimed_dir(tenant_id, worker_id).mkdir(parents=True, exist_ok=True)
        try:
            pending_file.replace(claimed_file)
        except FileNotFoundError:
            return None
        record = self._read_record(claimed_file)
        if record is None or record.status != "pending":
            claimed_file.unlink(missing_ok=True)
            return None
        claimed = replace(
            record,
            claim_token=uuid4().hex,
            claimed_at=now_iso(),
        )
        self._write_record(claimed_file, claimed)
        return claimed

    def list_pending(self, tenant_id: str, worker_id: str) -> tuple[SuggestionRecord, ...]:
        """List current pending suggestions."""
        self._recover_stale_claims(tenant_id, worker_id)
        return self._list_dir(self._pending_dir(tenant_id, worker_id))

    def list_resolved(self, tenant_id: str, worker_id: str) -> tuple[SuggestionRecord, ...]:
        """List resolved suggestions."""
        return self._list_dir(self._resolved_dir(tenant_id, worker_id))

    def resolve(
        self,
        tenant_id: str,
        worker_id: str,
        suggestion_id: str,
        *,
        status: str,
        resolved_by: str,
        resolution_note: str = "",
        claim_token: str = "",
    ) -> SuggestionRecord | None:
        """Resolve a pending suggestion and move it to resolved storage."""
        existing, source = self._read_claimed_or_pending(
            tenant_id,
            worker_id,
            suggestion_id,
            claim_token=claim_token,
        )
        if existing is None:
            return None
        updated = replace(
            existing,
            status=status,
            claim_token="",
            claimed_at="",
            resolved_at=now_iso(),
            resolved_by=resolved_by,
            resolution_note=resolution_note,
        )
        pending_file = self._pending_file(tenant_id, worker_id, suggestion_id)
        claimed_file = self._claimed_file(tenant_id, worker_id, suggestion_id)
        self._resolved_dir(tenant_id, worker_id).mkdir(parents=True, exist_ok=True)
        self._write_record(self._resolved_file(tenant_id, worker_id, suggestion_id), updated)
        if source == "pending" and pending_file.exists():
            pending_file.unlink()
        if source == "claimed" and claimed_file.exists():
            claimed_file.unlink()
        return updated

    def release_claim(
        self,
        tenant_id: str,
        worker_id: str,
        suggestion_id: str,
        *,
        claim_token: str = "",
    ) -> SuggestionRecord | None:
        """Return a claimed suggestion back to pending after a failed approval."""
        claimed_file = self._claimed_file(tenant_id, worker_id, suggestion_id)
        pending_file = self._pending_file(tenant_id, worker_id, suggestion_id)
        if not claimed_file.is_file():
            return None
        claimed = self._read_record(claimed_file)
        if claimed is None:
            claimed_file.unlink(missing_ok=True)
            return None
        if claimed.claim_token and claimed.claim_token != str(claim_token or ""):
            return None
        self._pending_dir(tenant_id, worker_id).mkdir(parents=True, exist_ok=True)
        try:
            claimed_file.replace(pending_file)
        except FileNotFoundError:
            return None
        released = replace(claimed, claim_token="", claimed_at="")
        self._write_record(pending_file, released)
        return released

    def touch_claim(
        self,
        tenant_id: str,
        worker_id: str,
        suggestion_id: str,
        *,
        claim_token: str = "",
    ) -> bool:
        """Refresh one claimed suggestion lease while approval is still running."""
        claimed_file = self._claimed_file(tenant_id, worker_id, suggestion_id)
        if not claimed_file.is_file():
            return False
        claimed = self._read_record(claimed_file)
        if claimed is None:
            claimed_file.unlink(missing_ok=True)
            return False
        if claimed.claim_token and claimed.claim_token != str(claim_token or ""):
            return False
        refreshed = replace(claimed, claimed_at=now_iso())
        self._write_record(claimed_file, refreshed)
        return True

    def mark_approval_applied(
        self,
        tenant_id: str,
        worker_id: str,
        suggestion_id: str,
        *,
        claim_token: str = "",
        summary: str = "",
        artifact_ref: str = "",
        stage: str = "materialized",
    ) -> SuggestionRecord | None:
        """Persist an approval checkpoint after side effects have completed."""
        claimed_file = self._claimed_file(tenant_id, worker_id, suggestion_id)
        if not claimed_file.is_file():
            return None
        claimed = self._read_record(claimed_file)
        if claimed is None:
            claimed_file.unlink(missing_ok=True)
            return None
        if claimed.claim_token and claimed.claim_token != str(claim_token or ""):
            return None
        checkpointed = replace(
            claimed,
            approval_stage=str(stage or "materialized"),
            approval_summary=str(summary or ""),
            approval_artifact_ref=str(artifact_ref or ""),
            approval_applied_at=now_iso(),
        )
        self._write_record(claimed_file, checkpointed)
        return checkpointed

    def claim_heartbeat_interval_seconds(self) -> float:
        """Return the background claim refresh interval."""
        timeout_seconds = float(self._CLAIM_TIMEOUT_SECONDS or 0)
        heartbeat_seconds = float(self._CLAIM_HEARTBEAT_SECONDS or 0)
        if timeout_seconds > 0:
            heartbeat_seconds = min(
                heartbeat_seconds or timeout_seconds / 3,
                timeout_seconds / 3,
            )
        return max(0.05, heartbeat_seconds or 30.0)

    def expire_pending(self, tenant_id: str, worker_id: str) -> tuple[SuggestionRecord, ...]:
        """Expire overdue pending suggestions."""
        self._recover_stale_claims(tenant_id, worker_id)
        expired: list[SuggestionRecord] = []
        now_dt = parse_iso(now_iso())
        for record in self.list_pending(tenant_id, worker_id):
            expires_at = parse_iso(record.expires_at)
            if expires_at is None or now_dt is None or expires_at >= now_dt:
                continue
            resolved = self.resolve(
                tenant_id,
                worker_id,
                record.suggestion_id,
                status="expired",
                resolved_by="system",
                resolution_note="auto_expired",
            )
            if resolved is not None:
                expired.append(resolved)
        return tuple(expired)

    def find_pending(
        self,
        tenant_id: str,
        worker_id: str,
        *,
        suggestion_type: str,
        source_entity_id: str,
    ) -> SuggestionRecord | None:
        """Find an existing pending suggestion by source + type."""
        self.expire_pending(tenant_id, worker_id)
        for record in self.list_pending(tenant_id, worker_id):
            if record.type == suggestion_type and record.source_entity_id == source_entity_id:
                return record
        return None

    def find_resolved(
        self,
        tenant_id: str,
        worker_id: str,
        *,
        suggestion_type: str,
        source_entity_id: str,
        statuses: Iterable[str] | None = None,
    ) -> SuggestionRecord | None:
        """Find a resolved suggestion by source + type, optionally filtered by status."""
        allowed_statuses = {
            str(item).strip()
            for item in (statuses or ())
            if str(item).strip()
        }
        for record in self.list_resolved(tenant_id, worker_id):
            if record.type != suggestion_type or record.source_entity_id != source_entity_id:
                continue
            if allowed_statuses and record.status not in allowed_statuses:
                continue
            return record
        return None

    def was_rejected_recently(
        self,
        tenant_id: str,
        worker_id: str,
        *,
        suggestion_type: str,
        source_entity_id: str,
        cooldown_days: int = 14,
    ) -> bool:
        """Check whether the source recently had a rejected suggestion."""
        now_dt = parse_iso(now_iso())
        if now_dt is None:
            return False
        for record in self.list_resolved(tenant_id, worker_id):
            if record.type != suggestion_type or record.source_entity_id != source_entity_id:
                continue
            if record.status != "rejected":
                continue
            resolved_at = parse_iso(record.resolved_at or record.created_at)
            if resolved_at is None:
                continue
            if (now_dt - resolved_at).days < cooldown_days:
                return True
        return False

    def _list_dir(self, directory: Path) -> tuple[SuggestionRecord, ...]:
        if not directory.is_dir():
            return ()
        records: list[SuggestionRecord] = []
        for file_path in sorted(directory.glob("*.json")):
            record = self._read_record(file_path)
            if record is not None:
                records.append(record)
        return tuple(records)

    def _read_record(self, file_path: Path) -> SuggestionRecord | None:
        try:
            return SuggestionRecord.from_dict(json.loads(file_path.read_text(encoding="utf-8")))
        except Exception as exc:
            logger.warning("[SuggestionStore] Failed to read %s: %s", file_path, exc)
            return None

    def _write_record(self, file_path: Path, record: SuggestionRecord) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = file_path.with_name(f".{file_path.name}.{uuid4().hex}.tmp")
        tmp_path.write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, file_path)

    def _read_claimed_or_pending(
        self,
        tenant_id: str,
        worker_id: str,
        suggestion_id: str,
        *,
        claim_token: str = "",
    ) -> tuple[SuggestionRecord | None, str]:
        claimed_file = self._claimed_file(tenant_id, worker_id, suggestion_id)
        if claimed_file.is_file():
            claimed = self._read_record(claimed_file)
            if claimed is None:
                return None, ""
            if claimed.claim_token and claimed.claim_token != str(claim_token or ""):
                return None, ""
            return claimed, "claimed"
        pending_file = self._pending_file(tenant_id, worker_id, suggestion_id)
        if pending_file.is_file():
            return self._read_record(pending_file), "pending"
        return None, ""

    def _read_claimed_pending_or_resolved(
        self,
        tenant_id: str,
        worker_id: str,
        suggestion_id: str,
    ) -> SuggestionRecord | None:
        for file_path in (
            self._claimed_file(tenant_id, worker_id, suggestion_id),
            self._pending_file(tenant_id, worker_id, suggestion_id),
            self._resolved_file(tenant_id, worker_id, suggestion_id),
        ):
            if file_path.is_file():
                return self._read_record(file_path)
        return None

    def _recover_stale_claims(self, tenant_id: str, worker_id: str) -> tuple[SuggestionRecord, ...]:
        """Return abandoned claimed suggestions back to pending."""
        claimed_dir = self._claimed_dir(tenant_id, worker_id)
        if not claimed_dir.is_dir():
            return ()
        self._pending_dir(tenant_id, worker_id).mkdir(parents=True, exist_ok=True)
        recovered: list[SuggestionRecord] = []
        now_dt = datetime.now(timezone.utc)
        for file_path in sorted(claimed_dir.glob("*.json")):
            record = self._read_record(file_path)
            if record is None:
                file_path.unlink(missing_ok=True)
                continue
            claimed_at = parse_iso(record.claimed_at)
            if claimed_at is None:
                try:
                    claimed_at = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
                except OSError:
                    continue
            try:
                claim_age_seconds = (now_dt - claimed_at).total_seconds()
            except TypeError:
                continue
            if claim_age_seconds < self._CLAIM_TIMEOUT_SECONDS:
                continue
            pending_file = self._pending_file(tenant_id, worker_id, file_path.stem)
            try:
                file_path.replace(pending_file)
            except FileNotFoundError:
                continue
            pending_record = self._read_record(pending_file)
            if pending_record is not None:
                cleared = replace(pending_record, claim_token="", claimed_at="")
                self._write_record(pending_file, cleared)
                recovered.append(cleared)
        return tuple(recovered)

    def _worker_dir(self, tenant_id: str, worker_id: str) -> Path:
        return self._workspace_root / "tenants" / tenant_id / "workers" / worker_id / "lifecycle"

    def _pending_dir(self, tenant_id: str, worker_id: str) -> Path:
        return self._worker_dir(tenant_id, worker_id) / "suggestions" / "pending"

    def _claimed_dir(self, tenant_id: str, worker_id: str) -> Path:
        return self._worker_dir(tenant_id, worker_id) / "suggestions" / "claimed"

    def _resolved_dir(self, tenant_id: str, worker_id: str) -> Path:
        return self._worker_dir(tenant_id, worker_id) / "suggestions" / "resolved"

    def _pending_file(self, tenant_id: str, worker_id: str, suggestion_id: str) -> Path:
        return self._pending_dir(tenant_id, worker_id) / f"{suggestion_id}.json"

    def _claimed_file(self, tenant_id: str, worker_id: str, suggestion_id: str) -> Path:
        return self._claimed_dir(tenant_id, worker_id) / f"{suggestion_id}.json"

    def _resolved_file(self, tenant_id: str, worker_id: str, suggestion_id: str) -> Path:
        return self._resolved_dir(tenant_id, worker_id) / f"{suggestion_id}.json"
