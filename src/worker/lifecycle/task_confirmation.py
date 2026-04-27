"""Helpers for gated derived task confirmation requests."""
from __future__ import annotations

import hashlib

from src.autonomy.inbox import InboxItem, SessionInboxStore
from src.worker.task import TaskManifest

CONFIRMATION_EVENT_TYPE = "task.confirmation_requested"


def confirmation_reason_for(task_description: str) -> str:
    """Build a user-facing confirmation reason for a gated task."""
    snippet = " ".join(str(task_description).split())[:80]
    if snippet:
        return f"派生任务涉及潜在写操作或外部副作用，需要人工确认后再执行: {snippet}"
    return "派生任务涉及潜在写操作或外部副作用，需要人工确认后再执行。"


async def enqueue_task_confirmation(
    *,
    inbox_store: SessionInboxStore,
    manifest: TaskManifest,
    task_description: str,
    preferred_skill_ids: tuple[str, ...] = (),
    target_session_key: str = "",
    reason: str = "",
    priority_hint: int = 0,
    task_kind: str = "task",
) -> InboxItem:
    """Write or reuse one pending confirmation item for a gated task."""
    dedupe_key = _confirmation_dedupe_key(
        manifest=manifest,
        task_description=task_description,
        task_kind=task_kind,
    )
    pending = await inbox_store.list_pending(
        tenant_id=manifest.tenant_id,
        worker_id=manifest.worker_id,
        event_type=CONFIRMATION_EVENT_TYPE,
        limit=200,
    )
    for item in pending:
        if item.dedupe_key == dedupe_key:
            return item

    item = InboxItem(
        tenant_id=manifest.tenant_id,
        worker_id=manifest.worker_id,
        target_session_key=target_session_key,
        source_type="task_confirmation",
        event_type=CONFIRMATION_EVENT_TYPE,
        priority_hint=max(priority_hint, 30),
        dedupe_key=dedupe_key,
        payload={
            "task_id": manifest.task_id,
            "task_kind": task_kind,
            "task_description": task_description,
            "reason": reason or confirmation_reason_for(task_description),
            "manifest": manifest.to_dict(),
            "preferred_skill_ids": list(preferred_skill_ids),
            "source_type": manifest.provenance.source_type,
            "source_id": manifest.provenance.source_id,
            "goal_id": manifest.provenance.goal_id,
            "duty_id": manifest.provenance.duty_id,
        },
    )
    return await inbox_store.write(item)


def _confirmation_dedupe_key(
    *,
    manifest: TaskManifest,
    task_description: str,
    task_kind: str,
) -> str:
    raw = "|".join(
        (
            manifest.worker_id,
            manifest.provenance.source_type,
            manifest.provenance.source_id,
            manifest.provenance.goal_id,
            manifest.provenance.duty_id,
            task_kind,
            " ".join(str(task_description).split()),
        )
    )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"task_confirmation:{digest}"
