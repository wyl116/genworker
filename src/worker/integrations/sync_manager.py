"""
SyncManager - bidirectional sync between local Goals and external sources.

Provides:
- sync_goal_progress: push local progress to external channel
- request_progress_update: ask stakeholders for status updates
- detect_conflict: compare local vs external content for conflicts
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Protocol

from src.channels.outbound_types import ChannelMessage, SenderScope
from src.worker.goal.models import Goal

from .domain_models import SyncRecord

logger = logging.getLogger(__name__)


class ChannelAdapterLike(Protocol):
    """Protocol for channel sending and document updates."""
    async def send(self, message: ChannelMessage) -> str: ...
    async def update_document(
        self,
        path: str,
        content: str,
        section: str | None = None,
        *,
        scope: SenderScope,
    ) -> bool: ...


class SyncManager:
    """Manage bidirectional sync between Goals and external systems."""

    def __init__(
        self,
        channel_adapter: ChannelAdapterLike,
        event_bus: object | None = None,
        tenant_id: str = "demo",
    ) -> None:
        self._channel = channel_adapter
        # These compatibility arguments remain accepted because bootstrap/tests
        # still pass them, but SyncManager no longer publishes events directly.
        _ = event_bus, tenant_id

    async def sync_goal_progress(
        self,
        goal: Goal,
        *,
        tenant_id: str,
        worker_id: str,
    ) -> SyncRecord:
        """
        Push goal progress to external source.

        Selects sync strategy based on goal.external_source.type:
        - email: send progress summary email to stakeholders
        - feishu_doc: update document section
        - others: send channel message
        """
        if goal.external_source is None:
            return _error_record(goal.goal_id, "no_channel", "No external source")

        source = goal.external_source
        channel = source.type
        now = _now_iso()

        try:
            if channel == "email":
                await self._sync_via_email(goal, tenant_id=tenant_id, worker_id=worker_id)
            elif channel == "feishu_doc":
                await self._sync_via_feishu(goal, tenant_id=tenant_id, worker_id=worker_id)
            else:
                await self._sync_via_message(
                    goal,
                    channel,
                    tenant_id=tenant_id,
                    worker_id=worker_id,
                )

            return SyncRecord(
                sync_id=_new_id("sync"),
                goal_id=goal.goal_id,
                direction="outbound",
                channel=channel,
                synced_at=now,
                status="success",
            )
        except Exception as exc:
            logger.error(
                f"[SyncManager] sync_goal_progress failed for "
                f"{goal.goal_id}: {exc}"
            )
            return SyncRecord(
                sync_id=_new_id("sync"),
                goal_id=goal.goal_id,
                direction="outbound",
                channel=channel,
                synced_at=now,
                status="error",
                detail=str(exc),
            )

    async def request_progress_update(
        self,
        goal: Goal,
        *,
        tenant_id: str,
        worker_id: str,
    ) -> None:
        """
        Ask stakeholders for progress updates via appropriate channel.

        Sends an inquiry message to all stakeholders listed in
        the goal's external source.
        """
        if goal.external_source is None:
            logger.warning(
                f"[SyncManager] No external source for goal {goal.goal_id}"
            )
            return

        source = goal.external_source
        recipients = source.stakeholders
        if not recipients:
            logger.warning(
                f"[SyncManager] No stakeholders for goal {goal.goal_id}"
            )
            return

        message = ChannelMessage(
            channel=source.type,
            recipients=recipients,
            subject=f"Progress inquiry: {goal.title}",
            content=(
                f"Please provide the latest progress update for "
                f"'{goal.title}'.\n\n"
                f"Current overall progress: "
                f"{goal.overall_progress:.0%}\n"
            ),
            message_type="progress_inquiry",
            sender_tenant_id=tenant_id,
            sender_worker_id=worker_id,
        )
        await self._channel.send(message)
        logger.info(
            f"[SyncManager] Sent progress inquiry for {goal.goal_id} "
            f"to {recipients}"
        )

    async def detect_conflict(
        self, goal: Goal, external_content: str,
    ) -> bool:
        """
        Detect conflicts between local Goal and external content.

        Returns True if conflict is detected (e.g., local says completed
        but external says in_progress). Does not auto-override.
        """
        if goal.external_source is None:
            return False

        local_summary = _build_local_summary(goal)
        has_conflict = _detect_content_conflict(local_summary, external_content)

        if has_conflict:
            logger.warning(
                f"[SyncManager] Conflict detected for goal "
                f"'{goal.goal_id}': local and external states diverge"
            )

        return has_conflict

    async def _sync_via_email(
        self,
        goal: Goal,
        *,
        tenant_id: str,
        worker_id: str,
    ) -> None:
        """Send progress summary email to stakeholders."""
        source = goal.external_source
        if source is None:
            return

        summary = _build_progress_summary(goal)
        message = ChannelMessage(
            channel="email",
            recipients=source.stakeholders,
            subject=f"Progress update: {goal.title}",
            content=summary,
            message_type="progress_update",
            sender_tenant_id=tenant_id,
            sender_worker_id=worker_id,
        )
        await self._channel.send(message)

    async def _sync_via_feishu(
        self,
        goal: Goal,
        *,
        tenant_id: str,
        worker_id: str,
    ) -> None:
        """Update Feishu document progress section."""
        source = goal.external_source
        if source is None:
            return

        summary = _build_progress_summary(goal)
        await self._channel.update_document(
            path=source.source_uri,
            content=summary,
            section="## Progress Update",
            scope=SenderScope(tenant_id=tenant_id, worker_id=worker_id),
        )

    async def _sync_via_message(
        self,
        goal: Goal,
        channel: str,
        *,
        tenant_id: str,
        worker_id: str,
    ) -> None:
        """Send progress message via generic channel."""
        source = goal.external_source
        if source is None:
            return

        summary = _build_progress_summary(goal)
        message = ChannelMessage(
            channel=channel,
            recipients=source.stakeholders,
            subject=f"Progress: {goal.title}",
            content=summary,
            message_type="progress_update",
            sender_tenant_id=tenant_id,
            sender_worker_id=worker_id,
        )
        await self._channel.send(message)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_progress_summary(goal: Goal) -> str:
    """Build a human-readable progress summary for a goal."""
    lines = [
        f"Goal: {goal.title}",
        f"Status: {goal.status}",
        f"Overall progress: {goal.overall_progress:.0%}",
        "",
        "Milestones:",
    ]
    for ms in goal.milestones:
        status_icon = {
            "completed": "[done]",
            "in_progress": "[in progress]",
            "pending": "[pending]",
        }.get(ms.status, f"[{ms.status}]")
        deadline_str = f" (due: {ms.deadline})" if ms.deadline else ""
        lines.append(
            f"  - {ms.title}: {status_icon} "
            f"{ms.progress_ratio:.0%}{deadline_str}"
        )
    return "\n".join(lines)


def _build_local_summary(goal: Goal) -> str:
    """Build a concise summary of local goal state for conflict detection."""
    parts: list[str] = [f"status={goal.status}"]
    for ms in goal.milestones:
        parts.append(f"{ms.title}={ms.status}")
    return "; ".join(parts)


def _detect_content_conflict(local_summary: str, external_content: str) -> bool:
    """
    Detect conflicts between local summary and external content.

    Simple heuristic: check for contradictory status keywords.
    """
    local_lower = local_summary.lower()
    external_lower = external_content.lower()

    # Check for obvious contradictions
    if "completed" in local_lower and "in progress" in external_lower:
        return True
    if "in_progress" in local_lower and "completed" in external_lower:
        return True
    if "completed" in local_lower and "not started" in external_lower:
        return True

    return False


def _error_record(goal_id: str, channel: str, detail: str) -> SyncRecord:
    """Create an error SyncRecord."""
    return SyncRecord(
        sync_id=_new_id("sync"),
        goal_id=goal_id,
        direction="outbound",
        channel=channel,
        synced_at=_now_iso(),
        status="error",
        detail=detail,
    )


def _new_id(prefix: str) -> str:
    """Generate a unique ID with prefix."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _now_iso() -> str:
    """Current time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()
