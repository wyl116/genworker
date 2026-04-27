# edition: baseline
"""
Tests for GoalGenerator - ParsedGoalInfo to Goal conversion.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.worker.goal.models import Goal
from src.worker.goal.parser import parse_goal
from src.worker.integrations.domain_models import ParsedGoalInfo
from src.worker.integrations.goal_generator import (
    _build_milestones,
    _sanitize_filename,
    find_goal_file,
    generate_goal_from_parsed,
    update_goal_from_external,
    write_goal_md,
)
from src.worker.scripts.models import InlineScript


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_parsed_info(**overrides) -> ParsedGoalInfo:
    """Create a ParsedGoalInfo with defaults."""
    defaults = dict(
        title="Q2 Data Migration",
        description="Migrate data to new platform",
        milestones=(
            {
                "title": "Design Phase",
                "deadline": "2026-04-15",
                "tasks": [
                    {"title": "Write spec"},
                    {"title": "Review spec"},
                ],
            },
            {
                "title": "Development",
                "deadline": "2026-05-30",
                "tasks": [
                    {"title": "Implement"},
                ],
            },
        ),
        deadline="2026-06-30",
        priority="high",
        stakeholders=("alice@company.com", "bob@company.com"),
        source_type="email",
        source_uri="email://inbox/42",
        raw_content="Original email content",
        confidence=0.85,
    )
    defaults.update(overrides)
    return ParsedGoalInfo(**defaults)


class MockEventBus:
    """Mock event bus that records published events."""

    def __init__(self):
        self.events: list = []

    async def publish(self, event) -> int:
        self.events.append(event)
        return 1


class MockLLMClient:
    """Mock LLM for update_goal_from_external."""

    def __init__(self, response: str) -> None:
        self._response = response

    async def invoke(self, messages, **kwargs):
        return self._response


# ---------------------------------------------------------------------------
# _build_milestones
# ---------------------------------------------------------------------------

class TestBuildMilestones:
    def test_basic_milestones(self):
        dicts = (
            {"title": "Phase 1", "deadline": "2026-04-15", "tasks": []},
            {"title": "Phase 2", "tasks": [{"title": "Task A"}]},
        )
        milestones = _build_milestones(dicts)

        assert len(milestones) == 2
        assert milestones[0].title == "Phase 1"
        assert milestones[0].deadline == "2026-04-15"
        assert milestones[0].status == "pending"
        assert len(milestones[0].tasks) == 0
        assert len(milestones[1].tasks) == 1
        assert milestones[1].tasks[0].title == "Task A"
        assert milestones[1].tasks[0].status == "pending"

    def test_empty_milestones(self):
        assert _build_milestones(()) == ()

    def test_tasks_get_generated_ids(self):
        dicts = ({"title": "MS1", "tasks": [{"title": "T1"}, {"title": "T2"}]},)
        milestones = _build_milestones(dicts)

        tasks = milestones[0].tasks
        assert tasks[0].id.startswith("ms-ext-1")
        assert tasks[1].id.startswith("ms-ext-1")
        assert tasks[0].id != tasks[1].id


# ---------------------------------------------------------------------------
# _sanitize_filename
# ---------------------------------------------------------------------------

class TestSanitizeFilename:
    def test_basic(self):
        assert _sanitize_filename("Hello World") == "hello-world"

    def test_special_chars(self):
        result = _sanitize_filename("Q2: Data Migration!")
        assert ":" not in result
        assert "!" not in result

    def test_max_length(self):
        long_title = "A" * 100
        assert len(_sanitize_filename(long_title)) <= 64


# ---------------------------------------------------------------------------
# generate_goal_from_parsed
# ---------------------------------------------------------------------------

class TestGenerateGoalFromParsed:
    @pytest.mark.asyncio
    async def test_creates_goal_with_correct_fields(self, tmp_path):
        parsed = _make_parsed_info()
        goal = await generate_goal_from_parsed(
            parsed=parsed,
            goals_dir=tmp_path / "goals",
            require_approval=True,
        )

        assert isinstance(goal, Goal)
        assert goal.title == "Q2 Data Migration"
        assert goal.priority == "high"
        assert goal.deadline == "2026-06-30"
        assert goal.status == "pending_approval"
        assert goal.goal_id.startswith("goal-ext-")

    @pytest.mark.asyncio
    async def test_require_approval_true_sets_pending(self, tmp_path):
        parsed = _make_parsed_info()
        goal = await generate_goal_from_parsed(
            parsed=parsed,
            goals_dir=tmp_path / "goals",
            require_approval=True,
        )
        assert goal.status == "pending_approval"

    @pytest.mark.asyncio
    async def test_require_approval_false_sets_active(self, tmp_path):
        parsed = _make_parsed_info()
        goal = await generate_goal_from_parsed(
            parsed=parsed,
            goals_dir=tmp_path / "goals",
            require_approval=False,
        )
        assert goal.status == "active"

    @pytest.mark.asyncio
    async def test_external_source_populated(self, tmp_path):
        parsed = _make_parsed_info()
        goal = await generate_goal_from_parsed(
            parsed=parsed,
            goals_dir=tmp_path / "goals",
        )

        assert goal.external_source is not None
        assert goal.external_source.type == "email"
        assert goal.external_source.source_uri == "email://inbox/42"
        assert goal.external_source.sync_direction == "bidirectional"
        assert "alice@company.com" in goal.external_source.stakeholders
        assert "bob@company.com" in goal.external_source.stakeholders

    @pytest.mark.asyncio
    async def test_milestones_converted(self, tmp_path):
        parsed = _make_parsed_info()
        goal = await generate_goal_from_parsed(
            parsed=parsed,
            goals_dir=tmp_path / "goals",
        )

        assert len(goal.milestones) == 2
        assert goal.milestones[0].title == "Design Phase"
        assert goal.milestones[0].deadline == "2026-04-15"
        assert len(goal.milestones[0].tasks) == 2
        assert goal.milestones[1].title == "Development"
        assert len(goal.milestones[1].tasks) == 1

    @pytest.mark.asyncio
    async def test_writes_goal_md_file(self, tmp_path):
        parsed = _make_parsed_info()
        goals_dir = tmp_path / "goals"

        await generate_goal_from_parsed(
            parsed=parsed,
            goals_dir=goals_dir,
        )

        files = list(goals_dir.glob("*.md"))
        assert len(files) == 1
        content = files[0].read_text(encoding="utf-8")
        assert "Q2 Data Migration" in content
        assert "pending_approval" in content
        assert "email" in content

    @pytest.mark.asyncio
    async def test_publishes_event(self, tmp_path):
        parsed = _make_parsed_info()
        event_bus = MockEventBus()

        goal = await generate_goal_from_parsed(
            parsed=parsed,
            goals_dir=tmp_path / "goals",
            event_bus=event_bus,
            tenant_id="test-tenant",
        )

        assert len(event_bus.events) == 1
        event = event_bus.events[0]
        assert event.type == "goal.created_from_external"
        assert event.tenant_id == "test-tenant"
        payload_dict = dict(event.payload)
        assert payload_dict["goal_id"] == goal.goal_id
        assert payload_dict["source_type"] == "email"
        assert payload_dict["confidence"] == 0.85

    @pytest.mark.asyncio
    async def test_no_event_when_no_bus(self, tmp_path):
        parsed = _make_parsed_info()
        goal = await generate_goal_from_parsed(
            parsed=parsed,
            goals_dir=tmp_path / "goals",
            event_bus=None,
        )
        assert goal is not None  # Should not raise

    @pytest.mark.asyncio
    async def test_creates_goals_dir_if_missing(self, tmp_path):
        parsed = _make_parsed_info()
        goals_dir = tmp_path / "deep" / "nested" / "goals"

        await generate_goal_from_parsed(
            parsed=parsed,
            goals_dir=goals_dir,
        )


class TestWriteGoalMd:
    def test_defaults_filename_to_goal_id(self, tmp_path):
        goal = Goal(
            goal_id="goal-123",
            title="My Goal",
            status="active",
            priority="high",
            milestones=(),
        )

        path = write_goal_md(goal, tmp_path / "goals")

        assert path.name == "goal-123.md"

    def test_reuses_existing_goal_file_for_same_goal_id(self, tmp_path):
        goals_dir = tmp_path / "goals"
        goal = Goal(
            goal_id="goal-123",
            title="My Goal",
            status="active",
            priority="high",
            milestones=(),
        )
        legacy = write_goal_md(goal, goals_dir, filename="legacy-title.md")

        updated = Goal(
            goal_id="goal-123",
            title="Updated Goal",
            status="active",
            priority="high",
            milestones=(),
        )
        path = write_goal_md(updated, goals_dir)

        assert path == legacy
        assert list(goals_dir.glob("*.md")) == [legacy]
        assert "Updated Goal" in legacy.read_text(encoding="utf-8")

    def test_find_goal_file_uses_parsed_goal_id(self, tmp_path):
        goals_dir = tmp_path / "goals"
        goal = Goal(
            goal_id="goal-123",
            title="My Goal",
            status="active",
            priority="high",
            milestones=(),
        )
        write_goal_md(goal, goals_dir, filename="legacy-title.md")

        found = find_goal_file(goals_dir, "goal-123")

        assert found is not None
        assert found.name == "legacy-title.md"

        assert goals_dir.exists()

    def test_write_goal_md_roundtrips_default_pre_script(self, tmp_path):
        goal = Goal(
            goal_id="goal-script",
            title="My Goal",
            status="active",
            priority="high",
            milestones=(),
            default_pre_script=InlineScript(
                source="print('goal context')",
                enabled_tools=("file_read",),
            ),
        )

        path = write_goal_md(goal, tmp_path / "goals", filename="goal-script.md")
        parsed = parse_goal(path.read_text(encoding="utf-8"))

        assert isinstance(parsed.default_pre_script, InlineScript)
        assert parsed.default_pre_script.source.strip() == "print('goal context')"
        assert parsed.default_pre_script.enabled_tools == ("file_read",)

    @pytest.mark.asyncio
    async def test_unknown_generated_status_raises_value_error(
        self, tmp_path, monkeypatch,
    ):
        parsed = _make_parsed_info()
        monkeypatch.setattr(
            "src.worker.integrations.goal_generator.ALLOWED_GOAL_STATUSES",
            frozenset({"active"}),
        )

        with pytest.raises(ValueError, match="coding error"):
            await generate_goal_from_parsed(
                parsed=parsed,
                goals_dir=tmp_path / "goals",
                require_approval=True,
            )


# ---------------------------------------------------------------------------
# update_goal_from_external
# ---------------------------------------------------------------------------

class TestUpdateGoalFromExternal:
    def _make_goal(self) -> Goal:
        from src.worker.goal.models import ExternalSource, Milestone
        return Goal(
            goal_id="goal-001",
            title="Test Goal",
            status="active",
            priority="high",
            milestones=(
                Milestone(id="ms-1", title="Phase 1", status="completed"),
                Milestone(id="ms-2", title="Phase 2", status="in_progress"),
            ),
            external_source=ExternalSource(
                type="email",
                source_uri="email://inbox/42",
            ),
        )

    @pytest.mark.asyncio
    async def test_no_conflict_preserves_goal(self):
        goal = self._make_goal()
        llm = MockLLMClient(json.dumps({
            "conflict": False,
            "updates": "No significant changes",
        }))

        result = await update_goal_from_external(goal, "new content", llm)
        assert result.goal_id == goal.goal_id
        assert result.status == "active"

    @pytest.mark.asyncio
    async def test_conflict_marks_external_source(self):
        goal = self._make_goal()
        llm = MockLLMClient(json.dumps({
            "conflict": True,
            "details": "Phase 1 status disagrees",
        }))

        result = await update_goal_from_external(goal, "conflicting content", llm)
        assert result.external_source is not None
        assert result.external_source.last_synced_at == "conflict_detected"

    @pytest.mark.asyncio
    async def test_llm_failure_returns_original(self):
        goal = self._make_goal()

        class FailLLM:
            async def invoke(self, messages, **kwargs):
                raise RuntimeError("LLM down")

        result = await update_goal_from_external(goal, "content", FailLLM())
        assert result is goal  # unchanged

    @pytest.mark.asyncio
    async def test_invalid_json_returns_original(self):
        goal = self._make_goal()
        llm = MockLLMClient("not valid json")

        result = await update_goal_from_external(goal, "content", llm)
        assert result is goal

    @pytest.mark.asyncio
    async def test_structured_updates_merge_goal_and_milestones(self):
        goal = self._make_goal()
        llm = MockLLMClient(json.dumps({
            "conflict": False,
            "goal": {
                "status": "paused",
                "deadline": "2026-05-31",
            },
            "milestones": [
                {
                    "id": "ms-2",
                    "status": "completed",
                    "tasks": [
                        {
                            "id": "ms-2-task-1",
                            "title": "Ship rollout",
                            "status": "completed",
                            "notes": "Confirmed externally",
                        },
                    ],
                },
                {
                    "title": "Phase 3",
                    "status": "pending",
                    "tasks": [
                        {
                            "title": "Validate adoption",
                            "status": "pending",
                        },
                    ],
                },
            ],
        }))

        result = await update_goal_from_external(goal, "new content", llm)

        assert result.status == "paused"
        assert result.deadline == "2026-05-31"
        assert len(result.milestones) == 3
        assert result.milestones[1].status == "completed"
        assert result.milestones[1].tasks[0].notes == "Confirmed externally"
        assert result.milestones[2].title == "Phase 3"
        assert result.external_source is not None
        assert result.external_source.last_synced_at is not None

    @pytest.mark.asyncio
    async def test_structured_remove_updates_delete_entities(self):
        from src.worker.goal.models import ExternalSource, GoalTask, Milestone

        goal = Goal(
            goal_id="goal-001",
            title="Test Goal",
            status="active",
            priority="high",
            milestones=(
                Milestone(
                    id="ms-1",
                    title="Phase 1",
                    status="completed",
                    tasks=(
                        GoalTask(id="t-1", title="Old Task", status="completed"),
                    ),
                ),
                Milestone(id="ms-2", title="Phase 2", status="in_progress"),
            ),
            external_source=ExternalSource(
                type="email",
                source_uri="email://inbox/42",
            ),
        )
        llm = MockLLMClient(json.dumps({
            "conflict": False,
            "milestones": [
                {"id": "ms-1", "action": "remove"},
            ],
        }))

        result = await update_goal_from_external(goal, "content", llm)

        assert len(result.milestones) == 1
        assert result.milestones[0].id == "ms-2"
