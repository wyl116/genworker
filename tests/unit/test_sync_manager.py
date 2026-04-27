# edition: baseline
"""
Tests for SyncManager - bidirectional sync between Goals and external systems.
"""
from __future__ import annotations

import pytest

from src.channels.outbound_types import ChannelMessage, SenderScope
from src.worker.goal.models import ExternalSource, Goal, Milestone
from src.worker.integrations.sync_manager import (
    SyncManager,
    _build_local_summary,
    _build_progress_summary,
    _detect_content_conflict,
)


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------

class MockChannelAdapter:
    """Mock channel adapter that records all operations."""

    def __init__(self, fail_send: bool = False) -> None:
        self.sent_messages: list[ChannelMessage] = []
        self.document_updates: list[dict] = []
        self._fail_send = fail_send

    async def send(self, message: ChannelMessage) -> str:
        if self._fail_send:
            raise RuntimeError("Send failed")
        self.sent_messages.append(message)
        return f"msg-{len(self.sent_messages)}"

    async def update_document(
        self,
        path: str,
        content: str,
        section: str | None = None,
        *,
        scope: SenderScope,
    ) -> bool:
        self.document_updates.append({
            "path": path,
            "content": content,
            "section": section,
            "scope": scope,
        })
        return True


class MockEventBus:
    """Mock event bus."""

    def __init__(self):
        self.events: list = []

    async def publish(self, event) -> int:
        self.events.append(event)
        return 1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_email_goal(**overrides) -> Goal:
    defaults = dict(
        goal_id="goal-001",
        title="Q2 Data Migration",
        status="active",
        priority="high",
        deadline="2026-06-30",
        milestones=(
            Milestone(
                id="ms-1", title="Design", status="completed",
                deadline="2026-04-15",
            ),
            Milestone(
                id="ms-2", title="Development", status="in_progress",
                deadline="2026-05-30",
            ),
        ),
        external_source=ExternalSource(
            type="email",
            source_uri="email://inbox/42",
            stakeholders=("alice@co.com", "bob@co.com"),
            sync_direction="bidirectional",
        ),
    )
    defaults.update(overrides)
    return Goal(**defaults)


def _make_feishu_goal(**overrides) -> Goal:
    defaults = dict(
        goal_id="goal-002",
        title="Feishu Project",
        status="active",
        priority="medium",
        milestones=(
            Milestone(id="ms-1", title="Phase 1", status="in_progress"),
        ),
        external_source=ExternalSource(
            type="feishu_doc",
            source_uri="mounts/feishu/project.md",
            stakeholders=("charlie@co.com",),
            sync_direction="bidirectional",
        ),
    )
    defaults.update(overrides)
    return Goal(**defaults)


# ---------------------------------------------------------------------------
# sync_goal_progress
# ---------------------------------------------------------------------------

class TestSyncGoalProgress:
    @pytest.mark.asyncio
    async def test_email_sync_sends_message(self):
        adapter = MockChannelAdapter()
        mgr = SyncManager(adapter)
        goal = _make_email_goal()

        record = await mgr.sync_goal_progress(
            goal,
            tenant_id="demo",
            worker_id="worker-1",
        )

        assert record.status == "success"
        assert record.direction == "outbound"
        assert record.channel == "email"
        assert record.goal_id == "goal-001"
        assert len(adapter.sent_messages) == 1
        msg = adapter.sent_messages[0]
        assert msg.channel == "email"
        assert "alice@co.com" in msg.recipients
        assert "Q2 Data Migration" in msg.subject
        assert msg.sender_tenant_id == "demo"
        assert msg.sender_worker_id == "worker-1"

    @pytest.mark.asyncio
    async def test_feishu_sync_updates_document(self):
        adapter = MockChannelAdapter()
        mgr = SyncManager(adapter)
        goal = _make_feishu_goal()

        record = await mgr.sync_goal_progress(
            goal,
            tenant_id="demo",
            worker_id="worker-2",
        )

        assert record.status == "success"
        assert record.channel == "feishu_doc"
        assert len(adapter.document_updates) == 1
        update = adapter.document_updates[0]
        assert update["path"] == "mounts/feishu/project.md"
        assert update["section"] == "## Progress Update"
        assert update["scope"] == SenderScope(tenant_id="demo", worker_id="worker-2")

    @pytest.mark.asyncio
    async def test_no_external_source_returns_error(self):
        adapter = MockChannelAdapter()
        mgr = SyncManager(adapter)
        goal = Goal(
            goal_id="goal-003",
            title="No Source",
            status="active",
            priority="low",
        )

        record = await mgr.sync_goal_progress(
            goal,
            tenant_id="demo",
            worker_id="worker-3",
        )
        assert record.status == "error"
        assert "No external source" in record.detail

    @pytest.mark.asyncio
    async def test_send_failure_returns_error_record(self):
        adapter = MockChannelAdapter(fail_send=True)
        mgr = SyncManager(adapter)
        goal = _make_email_goal()

        record = await mgr.sync_goal_progress(
            goal,
            tenant_id="demo",
            worker_id="worker-4",
        )
        assert record.status == "error"
        assert record.channel == "email"


# ---------------------------------------------------------------------------
# request_progress_update
# ---------------------------------------------------------------------------

class TestRequestProgressUpdate:
    @pytest.mark.asyncio
    async def test_sends_inquiry_message(self):
        adapter = MockChannelAdapter()
        mgr = SyncManager(adapter)
        goal = _make_email_goal()

        await mgr.request_progress_update(
            goal,
            tenant_id="demo",
            worker_id="worker-1",
        )

        assert len(adapter.sent_messages) == 1
        msg = adapter.sent_messages[0]
        assert msg.message_type == "progress_inquiry"
        assert "alice@co.com" in msg.recipients
        assert "Q2 Data Migration" in msg.content
        assert msg.sender_tenant_id == "demo"
        assert msg.sender_worker_id == "worker-1"

    @pytest.mark.asyncio
    async def test_no_external_source_does_nothing(self):
        adapter = MockChannelAdapter()
        mgr = SyncManager(adapter)
        goal = Goal(
            goal_id="goal-x",
            title="No Source",
            status="active",
            priority="low",
        )

        await mgr.request_progress_update(
            goal,
            tenant_id="demo",
            worker_id="worker-2",
        )
        assert len(adapter.sent_messages) == 0

    @pytest.mark.asyncio
    async def test_no_stakeholders_does_nothing(self):
        adapter = MockChannelAdapter()
        mgr = SyncManager(adapter)
        goal = Goal(
            goal_id="goal-x",
            title="No Stakeholders",
            status="active",
            priority="low",
            external_source=ExternalSource(
                type="email",
                source_uri="email://inbox/99",
                stakeholders=(),
            ),
        )

        await mgr.request_progress_update(
            goal,
            tenant_id="demo",
            worker_id="worker-3",
        )
        assert len(adapter.sent_messages) == 0


# ---------------------------------------------------------------------------
# detect_conflict
# ---------------------------------------------------------------------------

class TestDetectConflict:
    @pytest.mark.asyncio
    async def test_conflict_completed_vs_in_progress(self):
        adapter = MockChannelAdapter()
        event_bus = MockEventBus()
        mgr = SyncManager(adapter, event_bus=event_bus)
        goal = _make_email_goal(
            milestones=(
                Milestone(id="ms-1", title="Design", status="completed"),
            ),
        )

        result = await mgr.detect_conflict(
            goal,
            "Design phase is still in progress and needs more time",
        )

        assert result is True
        assert event_bus.events == []

    @pytest.mark.asyncio
    async def test_no_conflict_when_aligned(self):
        adapter = MockChannelAdapter()
        mgr = SyncManager(adapter)
        goal = _make_email_goal(
            milestones=(
                Milestone(id="ms-1", title="Design", status="in_progress"),
            ),
        )

        result = await mgr.detect_conflict(
            goal,
            "Design phase is going well, currently in progress",
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_no_external_source_returns_false(self):
        adapter = MockChannelAdapter()
        mgr = SyncManager(adapter)
        goal = Goal(
            goal_id="goal-x",
            title="No Source",
            status="active",
            priority="low",
        )

        result = await mgr.detect_conflict(goal, "some content")
        assert result is False


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

class TestBuildProgressSummary:
    def test_includes_goal_info(self):
        goal = _make_email_goal()
        summary = _build_progress_summary(goal)
        assert "Q2 Data Migration" in summary
        assert "active" in summary
        assert "Design" in summary
        assert "Development" in summary

    def test_includes_milestone_status(self):
        goal = _make_email_goal()
        summary = _build_progress_summary(goal)
        assert "[done]" in summary
        assert "[in progress]" in summary


class TestDetectContentConflict:
    def test_completed_vs_in_progress(self):
        assert _detect_content_conflict(
            "Design=completed", "Design is in progress"
        )

    def test_in_progress_vs_completed(self):
        assert _detect_content_conflict(
            "Design=in_progress", "Design completed successfully"
        )

    def test_no_conflict(self):
        assert not _detect_content_conflict(
            "Design=in_progress", "Design is going well"
        )

    def test_completed_vs_not_started(self):
        assert _detect_content_conflict(
            "Task=completed", "Task not started yet"
        )
