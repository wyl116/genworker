# edition: baseline
"""
Integration test: Feishu document sync flow.

Verifies the end-to-end flow from Feishu folder monitoring through
ContentParser to GoalGenerator, with bidirectional sync and
conflict detection.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from src.events.bus import EventBus, Subscription
from src.events.models import Event
from src.worker.goal.models import ExternalSource, Goal, Milestone
from src.channels.outbound import (
    FeishuChannelAdapter,
    MultiChannelFallback,
    ReliableChannelAdapter,
)
from src.channels.outbound_types import (
    ChannelMessage,
    ChannelPriority,
    RetryConfig,
)
from src.worker.integrations.content_parser import ContentParser
from src.worker.integrations.goal_generator import (
    generate_goal_from_parsed,
    update_goal_from_external,
)
from src.worker.integrations.sync_manager import SyncManager
from src.worker.sensing.sensors.feishu_file_sensor import FeishuFileSensor


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FileInfo:
    """Mock file info returned by list_directory."""
    name: str
    modified_at: str


class MockMountManager:
    """Mock mount manager for Feishu file operations."""

    def __init__(
        self,
        files: tuple[FileInfo, ...] = (),
        file_contents: dict[str, str] | None = None,
    ) -> None:
        self._files = files
        self._written: dict[str, str] = {}
        self._contents = file_contents or {}

    async def list_directory(self, virtual_path: str) -> tuple[FileInfo, ...]:
        return self._files

    async def read_file(self, path: str) -> str:
        return self._contents.get(path, "")

    async def write_file(self, path: str, content: str) -> None:
        self._written[path] = content


class MockLLMClient:
    """Mock LLM for content parsing and goal updates."""

    def __init__(self, response: str | dict) -> None:
        self._response = (
            json.dumps(response)
            if isinstance(response, dict)
            else response
        )

    async def invoke(self, messages, **kwargs):
        return self._response


class MockFeishuClient:
    """Mock Feishu client exposing list_with_metadata for modification checks."""

    def __init__(self, files: tuple[FileInfo, ...] = ()) -> None:
        self._files = files

    async def list_with_metadata(self, source, path, token):
        return tuple(
            type("Meta", (), {
                "name": item.name,
                "modified_at": item.modified_at,
                "path": f"{path}/{item.name}",
            })()
            for item in self._files
        )


# ---------------------------------------------------------------------------
# Feishu folder monitoring
# ---------------------------------------------------------------------------

class TestFeishuFolderMonitoring:
    @pytest.mark.asyncio
    async def test_detects_new_files(self):
        """FeishuFileSensor detects new files in Feishu folder."""
        files = (
            FileInfo(name="project-a.md", modified_at="2026-04-01T10:00:00"),
            FileInfo(name="project-b.md", modified_at="2026-04-02T10:00:00"),
        )
        sensor = FeishuFileSensor(
            mount_manager=MockMountManager(files=files),
            filter_config={"folder_path": "/docs"},
        )
        facts = await sensor.poll()
        assert len(facts) == 2

    @pytest.mark.asyncio
    async def test_detects_modified_files(self):
        """FeishuFileSensor detects modified files via feishu_client metadata."""
        initial_files = (
            FileInfo(name="project.md", modified_at="2026-04-01T10:00:00"),
        )
        feishu_client = MockFeishuClient(files=initial_files)
        sensor = FeishuFileSensor(
            feishu_client=feishu_client,
            filter_config={"folder_path": "/docs"},
        )

        # First poll
        await sensor.poll()

        # Update file timestamp
        updated_files = (
            FileInfo(name="project.md", modified_at="2026-04-02T10:00:00"),
        )
        feishu_client._files = updated_files

        # Second poll detects change
        changed = await sensor.poll()
        assert len(changed) == 1
        assert changed[0].payload_dict["name"] == "project.md"

    @pytest.mark.asyncio
    async def test_no_changes_returns_empty(self):
        """No changes detected when files are unchanged."""
        files = (
            FileInfo(name="project.md", modified_at="2026-04-01T10:00:00"),
        )
        sensor = FeishuFileSensor(
            mount_manager=MockMountManager(files=files),
            filter_config={"folder_path": "/docs"},
        )

        # First poll
        await sensor.poll()
        # Second poll - no changes
        changed = await sensor.poll()
        assert len(changed) == 0


# ---------------------------------------------------------------------------
# Feishu document sync
# ---------------------------------------------------------------------------

class TestFeishuDocumentSync:
    @pytest.mark.asyncio
    async def test_sync_updates_document_section(self):
        """SyncManager updates Feishu document progress section."""
        existing_doc = (
            "# Project\n\n"
            "## Overview\nProject overview text\n\n"
            "## Progress Update\nOld progress\n\n"
            "## Notes\nSome notes"
        )
        mount = MockMountManager(
            file_contents={"mounts/feishu/project.md": existing_doc},
        )
        feishu = FeishuChannelAdapter(mount)
        sync_mgr = SyncManager(channel_adapter=feishu)

        goal = Goal(
            goal_id="goal-f1",
            title="Feishu Project",
            status="active",
            priority="medium",
            milestones=(
                Milestone(id="ms-1", title="Phase 1", status="completed"),
                Milestone(id="ms-2", title="Phase 2", status="in_progress"),
            ),
            external_source=ExternalSource(
                type="feishu_doc",
                source_uri="mounts/feishu/project.md",
                stakeholders=("charlie@co.com",),
            ),
        )

        record = await sync_mgr.sync_goal_progress(
            goal,
            tenant_id="test",
            worker_id="worker-1",
        )

        assert record.status == "success"
        assert record.channel == "feishu_doc"
        # Check the document was updated
        updated = mount._written.get("mounts/feishu/project.md", "")
        assert "Phase 1" in updated or "Feishu Project" in updated

    @pytest.mark.asyncio
    async def test_conflict_detection_local_completed_external_in_progress(self):
        """Detect conflict when local says completed but external says in progress."""
        mount = MockMountManager()
        feishu = FeishuChannelAdapter(mount)
        event_bus = EventBus()
        sync_mgr = SyncManager(feishu, event_bus=event_bus)

        goal = Goal(
            goal_id="goal-f2",
            title="Sync Test",
            status="active",
            priority="high",
            milestones=(
                Milestone(id="ms-1", title="Design", status="completed"),
            ),
            external_source=ExternalSource(
                type="feishu_doc",
                source_uri="mounts/feishu/sync-test.md",
            ),
        )

        has_conflict = await sync_mgr.detect_conflict(
            goal,
            "Design phase is still in progress, needs more review",
        )

        assert has_conflict is True

    @pytest.mark.asyncio
    async def test_no_conflict_when_aligned(self):
        """No conflict when local and external agree."""
        mount = MockMountManager()
        feishu = FeishuChannelAdapter(mount)
        sync_mgr = SyncManager(feishu)

        goal = Goal(
            goal_id="goal-f3",
            title="Aligned",
            status="active",
            priority="medium",
            milestones=(
                Milestone(id="ms-1", title="Design", status="in_progress"),
            ),
            external_source=ExternalSource(
                type="feishu_doc",
                source_uri="mounts/feishu/aligned.md",
            ),
        )

        has_conflict = await sync_mgr.detect_conflict(
            goal,
            "Design phase is going well, team is working on it",
        )

        assert has_conflict is False


# ---------------------------------------------------------------------------
# Feishu goal update from external
# ---------------------------------------------------------------------------

class TestFeishuGoalUpdate:
    @pytest.mark.asyncio
    async def test_update_goal_conflict_marks_source(self):
        """External update with conflict marks goal for manual review."""
        goal = Goal(
            goal_id="goal-f4",
            title="Update Test",
            status="active",
            priority="medium",
            milestones=(
                Milestone(id="ms-1", title="Design", status="completed"),
            ),
            external_source=ExternalSource(
                type="feishu_doc",
                source_uri="mounts/feishu/update.md",
            ),
        )

        llm = MockLLMClient({
            "conflict": True,
            "details": "Design status disagrees",
        })

        updated = await update_goal_from_external(
            goal, "Design is still ongoing", llm,
        )

        assert updated.external_source is not None
        assert updated.external_source.last_synced_at == "conflict_detected"

    @pytest.mark.asyncio
    async def test_update_goal_no_conflict(self):
        """External update without conflict preserves goal state."""
        goal = Goal(
            goal_id="goal-f5",
            title="No Conflict",
            status="active",
            priority="medium",
            external_source=ExternalSource(
                type="feishu_doc",
                source_uri="mounts/feishu/noconflict.md",
            ),
        )

        llm = MockLLMClient({
            "conflict": False,
            "updates": "Minor clarification added",
        })

        updated = await update_goal_from_external(
            goal, "Updated content with minor clarification", llm,
        )

        assert updated.goal_id == goal.goal_id
        assert updated.status == "active"


# ---------------------------------------------------------------------------
# Full Feishu flow with channel adapter chain
# ---------------------------------------------------------------------------

class TestFeishuFullFlow:
    @pytest.mark.asyncio
    async def test_feishu_parse_to_goal_to_sync(self, tmp_path):
        """End-to-end: Feishu doc -> parse -> goal -> sync back."""
        # 1. Parse Feishu document content
        llm = MockLLMClient({
            "title": "Feishu Project Plan",
            "description": "Project tracked via Feishu",
            "milestones": [
                {"title": "Planning", "deadline": "2026-04-20", "tasks": []},
            ],
            "deadline": "2026-06-01",
            "priority": "medium",
            "stakeholders": ["pm@co.com"],
            "confidence": 0.9,
        })
        parser = ContentParser(llm, confidence_threshold=0.6)

        parsed = await parser.parse(
            content="Feishu project document content...",
            source_type="feishu_doc",
            context={"source_uri": "mounts/feishu/plan.md"},
        )
        assert parsed is not None

        # 2. Create goal
        event_bus = EventBus()
        goal = await generate_goal_from_parsed(
            parsed=parsed,
            goals_dir=tmp_path / "goals",
            require_approval=False,
            event_bus=event_bus,
            tenant_id="test",
        )
        assert goal.status == "active"
        assert goal.external_source.type == "feishu_doc"

        # 3. Sync back to Feishu
        existing_doc = "# Plan\n\n## Progress Update\nNo updates yet\n\n## End"
        mount = MockMountManager(
            file_contents={"mounts/feishu/plan.md": existing_doc},
        )
        feishu = FeishuChannelAdapter(mount)
        reliable = ReliableChannelAdapter(
            inner=feishu,
            retry_config=RetryConfig(max_retries=1, backoff_base=0.01),
        )

        sync_mgr = SyncManager(channel_adapter=reliable)
        record = await sync_mgr.sync_goal_progress(
            goal,
            tenant_id="test",
            worker_id="worker-2",
        )

        assert record.status == "success"
        assert record.channel == "feishu_doc"
