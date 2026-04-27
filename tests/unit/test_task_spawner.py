# edition: baseline
"""
Unit tests for TaskSpawner - spawn_task tool executor.

Tests input validation, task creation, and rejection scenarios.
"""
import pytest

from src.conversation.models import ConversationSession
from src.conversation.task_spawner import (
    SPAWN_TASK_TOOL_SCHEMA,
    SpawnTaskInput,
    SpawnTaskResult,
    TaskSpawner,
)
from src.events.bus import EventBus, Subscription
from src.skills.models import Skill
from src.skills.registry import SkillRegistry
from src.worker.registry import WorkerEntry, build_worker_registry
from src.worker.models import Worker, WorkerIdentity
from src.worker.task import TaskStore


class _AcceptingScheduler:
    def __init__(self) -> None:
        self.jobs = []

    async def submit_task(self, job, priority):
        self.jobs.append((job, priority))
        return True


class _RejectingScheduler:
    async def submit_task(self, job, priority):
        return False


class TestSpawnTaskInputFrozen:
    """SpawnTaskInput must be a frozen dataclass."""

    def test_immutable(self):
        inp = SpawnTaskInput(task_description="test", context="ctx")
        with pytest.raises(AttributeError):
            inp.task_description = "modified"

    def test_defaults(self):
        inp = SpawnTaskInput(task_description="test")
        assert inp.context == ""
        assert inp.skill_hint is None


class TestSpawnTaskResultFrozen:
    """SpawnTaskResult must be a frozen dataclass."""

    def test_immutable(self):
        result = SpawnTaskResult(
            task_id="t1", status="accepted", message="ok",
        )
        with pytest.raises(AttributeError):
            result.status = "rejected"


class TestSpawnTaskToolSchema:
    """SPAWN_TASK_TOOL_SCHEMA has the required structure."""

    def test_schema_name(self):
        assert SPAWN_TASK_TOOL_SCHEMA["name"] == "spawn_task"

    def test_schema_has_required_fields(self):
        required = SPAWN_TASK_TOOL_SCHEMA["parameters"]["required"]
        assert "task_description" in required
        assert "context" in required

    def test_schema_properties(self):
        props = SPAWN_TASK_TOOL_SCHEMA["parameters"]["properties"]
        assert "task_description" in props
        assert "context" in props
        assert "skill_hint" in props


class TestTaskSpawnerExecute:
    """Tests for TaskSpawner.execute()."""

    @pytest.fixture
    def session(self) -> ConversationSession:
        return ConversationSession(
            session_id="s1",
            thread_id="t1",
            tenant_id="demo",
            worker_id="w1",
        )

    @pytest.fixture
    def task_store(self, tmp_path) -> TaskStore:
        return TaskStore(workspace_root=tmp_path)

    @pytest.mark.asyncio
    async def test_accept_valid_task(self, session, task_store):
        scheduler = _AcceptingScheduler()
        spawner = TaskSpawner(task_store=task_store, worker_schedulers={"w1": scheduler})

        result = await spawner.execute(
            input_data=SpawnTaskInput(
                task_description="Generate quarterly report",
                context="Q1 2026, by region",
            ),
            session=session,
        )

        assert result.status == "accepted"
        assert result.task_id  # non-empty
        assert "created" in result.message.lower() or result.task_id in result.message
        assert len(scheduler.jobs) == 1

    @pytest.mark.asyncio
    async def test_reject_empty_description(self, session, task_store):
        spawner = TaskSpawner(task_store=task_store)

        result = await spawner.execute(
            input_data=SpawnTaskInput(
                task_description="",
                context="some context",
            ),
            session=session,
        )

        assert result.status == "rejected"
        assert result.task_id == ""

    @pytest.mark.asyncio
    async def test_reject_whitespace_only_description(self, session, task_store):
        spawner = TaskSpawner(task_store=task_store)

        result = await spawner.execute(
            input_data=SpawnTaskInput(
                task_description="   ",
                context="some context",
            ),
            session=session,
        )

        assert result.status == "rejected"

    @pytest.mark.asyncio
    async def test_reject_invalid_skill_hint(self, session, task_store):
        """When skill_registry is provided and skill_hint is invalid, reject."""

        class FakeRegistry:
            def get(self, skill_id):
                return None

        spawner = TaskSpawner(
            task_store=task_store,
            skill_registry=FakeRegistry(),
        )

        result = await spawner.execute(
            input_data=SpawnTaskInput(
                task_description="Generate report",
                context="ctx",
                skill_hint="nonexistent-skill",
            ),
            session=session,
        )

        assert result.status == "rejected"
        assert "nonexistent-skill" in result.message

    @pytest.mark.asyncio
    async def test_accept_valid_skill_hint(self, session, task_store):
        """When skill_hint matches a registered skill, accept."""

        class FakeRegistry:
            def get(self, skill_id):
                if skill_id == "report-gen":
                    return Skill(skill_id="report-gen", name="Report", gate_level="auto")
                return None

        spawner = TaskSpawner(
            task_store=task_store,
            skill_registry=FakeRegistry(),
            worker_schedulers={"w1": _AcceptingScheduler()},
        )

        result = await spawner.execute(
            input_data=SpawnTaskInput(
                task_description="Generate report",
                context="ctx",
                skill_hint="report-gen",
            ),
            session=session,
        )

        assert result.status == "accepted"
        manifest = task_store.load("demo", "w1", result.task_id)
        assert manifest is not None
        assert manifest.gate_level == "auto"

    @pytest.mark.asyncio
    async def test_task_manifest_created_and_pending_before_execution(self, session, task_store):
        """Accepted spawned task should stay pending until the runner starts it."""
        scheduler = _AcceptingScheduler()
        spawner = TaskSpawner(task_store=task_store, worker_schedulers={"w1": scheduler})

        result = await spawner.execute(
            input_data=SpawnTaskInput(
                task_description="Analyze data",
                context="Q1 sales data",
            ),
            session=session,
        )

        assert result.status == "accepted"

        # Verify the manifest is persisted
        from src.worker.task import TaskStatus
        manifest = task_store.load("demo", "w1", result.task_id)
        assert manifest is not None
        assert manifest.status == TaskStatus.PENDING
        assert "Analyze data" in manifest.task_description
        assert len(scheduler.jobs) == 1

    @pytest.mark.asyncio
    async def test_spawned_task_propagates_main_session_key(self, task_store):
        scheduler = _AcceptingScheduler()
        spawner = TaskSpawner(task_store=task_store, worker_schedulers={"w1": scheduler})
        session = ConversationSession(
            session_id="s-main",
            thread_id="main:w1",
            tenant_id="demo",
            worker_id="w1",
            session_type="main",
            main_session_key="main:w1",
        )

        result = await spawner.execute(
            input_data=SpawnTaskInput(
                task_description="Prepare follow-up summary",
                context="Use the latest heartbeat context",
            ),
            session=session,
        )

        assert result.status == "accepted"
        manifest = task_store.load("demo", "w1", result.task_id)
        assert manifest is not None
        assert manifest.main_session_key == "main:w1"
        job, priority = scheduler.jobs[0]
        assert priority == 20
        assert job["thread_id"] == "main:w1"
        assert job["main_session_key"] == "main:w1"

    @pytest.mark.asyncio
    async def test_no_skill_registry_accepts_any_hint(self, session, task_store):
        """Without a skill_registry, skill_hint is not validated."""
        spawner = TaskSpawner(
            task_store=task_store,
            skill_registry=None,
            worker_schedulers={"w1": _AcceptingScheduler()},
        )

        result = await spawner.execute(
            input_data=SpawnTaskInput(
                task_description="Run analysis",
                context="ctx",
                skill_hint="any-skill",
            ),
            session=session,
        )

        assert result.status == "accepted"

    @pytest.mark.asyncio
    async def test_worker_registry_skill_overrides_global_registry(self, session, task_store):
        global_registry = SkillRegistry.from_skills(())
        worker_skill = Skill(skill_id="worker-only", name="Worker Skill", gate_level="auto")
        worker_registry = build_worker_registry(
            [
                WorkerEntry(
                    worker=Worker(
                        identity=WorkerIdentity(worker_id="w1", name="Worker One")
                    ),
                    skill_registry=SkillRegistry.from_skills((worker_skill,)),
                )
            ],
            default_worker_id="w1",
        )
        spawner = TaskSpawner(
            task_store=task_store,
            skill_registry=global_registry,
            worker_registry=worker_registry,
            worker_schedulers={"w1": _AcceptingScheduler()},
        )

        result = await spawner.execute(
            input_data=SpawnTaskInput(
                task_description="Run worker-only skill",
                context="ctx",
                skill_hint="worker-only",
            ),
            session=session,
        )

        assert result.status == "accepted"
        manifest = task_store.load("demo", "w1", result.task_id)
        assert manifest is not None
        assert manifest.skill_id == "worker-only"
        assert manifest.gate_level == "auto"

    @pytest.mark.asyncio
    async def test_missing_scheduler_rejects_and_marks_task_error(self, session, task_store):
        spawner = TaskSpawner(task_store=task_store)

        result = await spawner.execute(
            input_data=SpawnTaskInput(
                task_description="Generate quarterly report",
                context="Q1 2026, by region",
            ),
            session=session,
        )

        assert result.status == "rejected"
        assert "scheduler" in result.message.lower()
        manifests = task_store.list_by_worker("demo", "w1")
        assert len(manifests) == 1
        assert manifests[0].status.value == "error"
        assert manifests[0].error_message == "Worker scheduler is not available"

    @pytest.mark.asyncio
    async def test_scheduler_rejection_marks_task_error(self, session, task_store):
        spawner = TaskSpawner(
            task_store=task_store,
            worker_schedulers={"w1": _RejectingScheduler()},
        )

        result = await spawner.execute(
            input_data=SpawnTaskInput(
                task_description="Generate quarterly report",
                context="Q1 2026, by region",
            ),
            session=session,
        )

        assert result.status == "rejected"
        manifests = task_store.list_by_worker("demo", "w1")
        assert len(manifests) == 1
        assert manifests[0].status.value == "error"
        assert manifests[0].error_message == "Scheduler quota exhausted"

    @pytest.mark.asyncio
    async def test_spawn_task_failure_event_includes_thread_id(self, task_store):
        captured = []

        async def _on_failed(event):
            captured.append(dict(event.payload))

        event_bus = EventBus()
        event_bus.subscribe(
            Subscription(
                handler_id="test-task-spawner-failed",
                event_type="task.failed",
                tenant_id="demo",
                handler=_on_failed,
            )
        )
        spawner = TaskSpawner(
            task_store=task_store,
            worker_schedulers={"w1": _RejectingScheduler()},
            event_bus=event_bus,
        )
        session = ConversationSession(
            session_id="s-main",
            thread_id="main:w1",
            tenant_id="demo",
            worker_id="w1",
            session_type="main",
            main_session_key="main:w1",
        )

        result = await spawner.execute(
            input_data=SpawnTaskInput(
                task_description="Generate quarterly report",
                context="Q1 2026, by region",
            ),
            session=session,
        )

        assert result.status == "rejected"
        assert captured
        assert captured[0]["session_id"] == "s-main"
        assert captured[0]["thread_id"] == "main:w1"
        assert captured[0]["error_code"] == "QUOTA_EXHAUSTED"
