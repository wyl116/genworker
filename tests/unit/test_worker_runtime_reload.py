# edition: baseline
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from src.api.app import _MissingEngineDispatcher, _store_dependencies
from src.autonomy.isolated_run import IsolatedRunManager
from src.autonomy.inbox import SessionInboxStore
from src.bootstrap.context import BootstrapContext
from src.channels.commands.approval_events import approval_event_types
from src.conversation.task_spawner import TaskSpawner
from src.events.bus import EventBus
from src.worker.lifecycle.feedback_store import FeedbackStore
from src.worker.lifecycle.suggestion_store import SuggestionStore
from src.worker.loader import load_worker_entry
from src.worker.registry import build_worker_registry
from src.worker.task import TaskStore


@dataclass
class _FakeJob:
    id: str
    args: tuple = ()


class _FakeScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, _FakeJob] = {}

    def add_job(self, func, *args, **kwargs) -> None:
        job_id = kwargs["id"]
        job_args = tuple(kwargs.get("args", ()) or ())
        self.jobs[job_id] = _FakeJob(id=job_id, args=job_args)

    def remove_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)

    def get_jobs(self) -> list[_FakeJob]:
        return list(self.jobs.values())


class _FakeHeartbeatRunner:
    def __init__(self) -> None:
        self.strategy = None
        self.worker_scheduler = None
        self.isolated_run_manager = None
        self.worker_router = None

    def update_strategy(self, strategy) -> None:
        self.strategy = strategy

    def replace_worker_router(self, worker_router) -> None:
        self.worker_router = worker_router

    def replace_runtime_dependencies(
        self,
        *,
        worker_scheduler=None,
        isolated_run_manager=None,
    ) -> None:
        self.worker_scheduler = worker_scheduler
        self.isolated_run_manager = isolated_run_manager


class _StubPlatformClientFactory:
    def __init__(self, mapping: dict[str, object] | None = None) -> None:
        self.mapping = mapping or {}

    def get_client(self, tenant_id: str, worker_id: str, channel_type: str):
        return self.mapping.get(channel_type)

    def invalidate(self, tenant_id: str | None = None, worker_id: str | None = None) -> None:
        return None


class _RouterWithRuntimeSetters:
    def __init__(self, registry) -> None:
        self._worker_registry = registry
        self._contact_registries = {}
        self.session_search_index = None
        self.task_spawner = None

    def set_session_search_index(self, search_index) -> None:
        self.session_search_index = search_index

    def set_task_spawner(self, task_spawner) -> None:
        self.task_spawner = task_spawner


class _FakeChannelRouter:
    def __init__(self) -> None:
        self.runtime_dependency_updates: list[dict[str, object]] = []
        self.contact_extractors = None
        self.worker_router = None

    def replace_contact_extractors(self, contact_extractors) -> None:
        self.contact_extractors = contact_extractors

    def replace_worker_router(self, worker_router) -> None:
        self.worker_router = worker_router

    def replace_runtime_dependencies(self, **kwargs) -> None:
        self.runtime_dependency_updates.append(kwargs)


def _write_persona(
    path: Path,
    *,
    name: str,
    goal_task_action: str,
    include_monitor: bool,
    include_contact: bool,
    channel_chat_id: str | None = None,
) -> None:
    monitor_block = ""
    if include_monitor:
        monitor_block = (
            "sensor_configs:\n"
            "  - source_type: email\n"
            "    poll_interval: \"15m\"\n"
            "    auto_create_goal: true\n"
            "    require_approval: false\n"
            "    filter:\n"
            "      subject_keywords: \"Project\"\n"
        )
    contacts_block = ""
    if include_contact:
        contacts_block = (
            "contacts:\n"
            "  - person_id: sponsor-1\n"
            "    name: Alice\n"
            "    role: Sponsor\n"
            "    identities:\n"
            "      - channel_type: email\n"
            "        handle: alice@example.com\n"
            "        email: alice@example.com\n"
        )
    channels_block = ""
    if channel_chat_id:
        channels_block = (
            "channels:\n"
            "  - type: feishu\n"
            "    connection_mode: webhook\n"
            "    chat_ids:\n"
            f"      - {channel_chat_id}\n"
            "    reply_mode: complete\n"
        )
    path.write_text(
        (
            "---\n"
            "identity:\n"
            "  worker_id: analyst-01\n"
            f"  name: {name}\n"
            "  role: analyst\n"
            "default_skill: general-query\n"
            "heartbeat:\n"
            "  goal_task_actions:\n"
            f"    - {goal_task_action}\n"
            f"{monitor_block}"
            f"{contacts_block}"
            f"{channels_block}"
            "---\n"
            "Analyst instructions.\n"
        ),
        encoding="utf-8",
    )


def _write_duty(path: Path) -> None:
    path.write_text(
        (
            "---\n"
            "duty_id: daily-review\n"
            "title: Daily Review\n"
            "status: active\n"
            "triggers:\n"
            "  - id: morning\n"
            "    type: schedule\n"
            "    cron: \"0 9 * * *\"\n"
            "quality_criteria:\n"
            "  - complete\n"
            "---\n"
            "Review the latest status.\n"
        ),
        encoding="utf-8",
    )


def _write_event_duty(path: Path, *, duty_id: str = "event-review") -> None:
    path.write_text(
        (
            "---\n"
            f"duty_id: {duty_id}\n"
            "title: Event Review\n"
            "status: active\n"
            "triggers:\n"
            "  - id: inbox-event\n"
            "    type: event\n"
            "    source: data.file_uploaded\n"
            "quality_criteria:\n"
            "  - complete\n"
            "---\n"
            "Review uploaded data.\n"
        ),
        encoding="utf-8",
    )


def _write_goal(path: Path, *, goal_id: str = "goal-1", title: str = "Reduce risk") -> None:
    path.write_text(
        (
            "---\n"
            f"goal_id: {goal_id}\n"
            f"title: {title}\n"
            "status: active\n"
            "priority: high\n"
            "milestones:\n"
            "  - id: ms-1\n"
            "    title: First milestone\n"
            "    status: pending\n"
            "    tasks:\n"
            "      - id: task-1\n"
            "        title: Investigate\n"
            "        status: pending\n"
            "external_source:\n"
            "  type: email\n"
            "  source_uri: email://thread/1\n"
            "  sync_schedule: 15m\n"
            "---\n"
            "Goal body.\n"
        ),
        encoding="utf-8",
    )


def _write_langgraph_skill(path: Path, *, event_type: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            "---\n"
            "skill_id: approval-reload\n"
            "strategy:\n"
            "  mode: langgraph\n"
            "  graph:\n"
            "    state_schema:\n"
            "      task: str\n"
            "    entry: human_approval\n"
            "    nodes:\n"
            "      - name: human_approval\n"
            "        kind: interrupt\n"
            "        prompt_ref: approval_prompt\n"
            f"        inbox_event_type: {event_type}\n"
            "    edges: []\n"
            "---\n\n"
            "## instructions.approval_prompt\n"
            "审批 {task}\n"
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_missing_engine_dispatcher_raises_explicit_runtime_error():
    dispatcher = _MissingEngineDispatcher()

    with pytest.raises(RuntimeError, match="engine_dispatcher not available"):
        async for _ in dispatcher.dispatch():
            pass


@pytest.mark.asyncio
async def test_reload_worker_runtime_refreshes_local_subsystems(tmp_path: Path):
    workspace_root = tmp_path / "workspace"
    worker_dir = workspace_root / "tenants" / "demo" / "workers" / "analyst-01"
    (workspace_root / "system" / "skills").mkdir(parents=True, exist_ok=True)
    (workspace_root / "tenants" / "demo" / "skills").mkdir(parents=True, exist_ok=True)
    (worker_dir / "skills").mkdir(parents=True, exist_ok=True)
    (worker_dir / "duties").mkdir(parents=True, exist_ok=True)
    (worker_dir / "goals").mkdir(parents=True, exist_ok=True)

    persona_path = worker_dir / "PERSONA.md"
    _write_persona(
        persona_path,
        name="Analyst One",
        goal_task_action="escalate",
        include_monitor=False,
        include_contact=False,
    )
    _write_langgraph_skill(
        worker_dir / "skills" / "approval-reload" / "SKILL.md",
        event_type="reload_order_approval",
    )
    _write_duty(worker_dir / "duties" / "daily-review.md")
    _write_goal(worker_dir / "goals" / "goal-1.md")

    entry = load_worker_entry(
        workspace_root=workspace_root,
        tenant_id="demo",
        worker_id="analyst-01",
    )
    registry = build_worker_registry([entry], default_worker_id="analyst-01")

    fake_scheduler = _FakeScheduler()
    fake_scheduler.jobs["goal:legacy-goal:health_check"] = _FakeJob(
        id="goal:legacy-goal:health_check",
        args=(worker_dir / "goals" / "goal-1.md", "demo", "analyst-01"),
    )
    fake_scheduler.jobs["system:sharing-cycle:analyst-01"] = _FakeJob(
        id="system:sharing-cycle:analyst-01",
        args=("demo", "analyst-01", worker_dir, None),
    )
    heartbeat_runner = _FakeHeartbeatRunner()
    worker_router = SimpleNamespace(
        _worker_registry=registry,
        _contact_registries={},
    )

    settings = SimpleNamespace(
        heartbeat_goal_task_actions="escalate,recover,investigate",
        heartbeat_goal_isolated_actions="replan,deep_review",
        heartbeat_goal_isolated_deviation_threshold=0.9,
    )
    context = BootstrapContext(settings=settings)
    context.set_state("workspace_root", workspace_root)
    context.set_state("worker_registry", registry)
    context.set_state("worker_router", worker_router)
    context.set_state("event_bus", EventBus())
    context.set_state("apscheduler", fake_scheduler)
    context.set_state("trigger_managers", {})
    context.set_state("worker_schedulers", {})
    context.set_state("heartbeat_runners", {"analyst-01": heartbeat_runner})
    context.set_state("contact_registries", {})
    context.set_state("sensor_registries", {})
    context.set_state("suggestion_store", SuggestionStore(workspace_root))
    context.set_state("feedback_store", FeedbackStore(workspace_root))
    goal_inbox_store = SessionInboxStore(redis_client=None, fallback_dir=workspace_root)
    context.set_state("goal_inbox_store", goal_inbox_store)
    engine_dispatcher = SimpleNamespace(langgraph_engine=object())
    context.set_state("engine_dispatcher", engine_dispatcher)
    isolated_run_manager = IsolatedRunManager(
        task_store=TaskStore(workspace_root),
        worker_schedulers={},
    )
    context.set_state("isolated_run_manager", isolated_run_manager)
    task_spawner = TaskSpawner(
        task_store=TaskStore(workspace_root),
        worker_schedulers={},
        event_bus=context.get_state("event_bus"),
    )
    context.set_state("task_spawner", task_spawner)
    context.set_state(
        "platform_client_factory",
        _StubPlatformClientFactory({"email": object()}),
    )
    channel_router = _FakeChannelRouter()
    context.set_state("channel_message_router", channel_router)
    context.set_state("channel_router", channel_router)

    app = FastAPI()
    _store_dependencies(app, context)

    _write_persona(
        persona_path,
        name="Analyst Reloaded",
        goal_task_action="recover",
        include_monitor=True,
        include_contact=True,
    )

    result = await app.state.reload_worker_runtime(
        worker_id="analyst-01",
        tenant_id="demo",
    )

    assert result["worker_id"] == "analyst-01"
    assert result["name"] == "Analyst Reloaded"
    assert result["heartbeat_runner_refreshed"] is True
    assert result["contact_registry_refreshed"] is True
    assert result["trigger_manager_refreshed"] is True
    assert result["goal_checks_refreshed"] is True
    assert result["recurring_jobs_refreshed"] is True
    assert result["sensor_registry_refreshed"] is True

    updated_entry = app.state.worker_registry.get("analyst-01")
    assert updated_entry is not None
    assert updated_entry.worker.name == "Analyst Reloaded"

    contact_registry = app.state.contact_registries["analyst-01"]
    contact_names = [contact.primary_name for contact in contact_registry.list_contacts()]
    assert "Alice" in contact_names

    sensor_snapshot = app.state.sensor_registries["analyst-01"].health
    assert sensor_snapshot["sensor_count"] == 1
    assert "email" in sensor_snapshot["sensors"]

    trigger_snapshot = app.state.trigger_managers["analyst-01"].registration_snapshot
    assert trigger_snapshot["duty_count"] == 1
    assert trigger_snapshot["resource_count"] == 1
    assert app.state.trigger_managers["analyst-01"]._duty_executor._duty_learning_handler is not None
    assert (
        app.state.worker_schedulers["analyst-01"]._side_effects._dead_letter_store
        is not None
    )
    assert app.state.worker_schedulers["analyst-01"]._engine_dispatcher is engine_dispatcher
    assert app.state.worker_schedulers["analyst-01"]._inbox_store is goal_inbox_store

    assert "goal:legacy-goal:health_check" not in fake_scheduler.jobs
    assert "goal:analyst-01:goal-1:health_check" in fake_scheduler.jobs
    assert "system:profile-update:analyst-01" in fake_scheduler.jobs
    assert "system:crystallization:analyst-01" in fake_scheduler.jobs
    assert "system:task-pattern:analyst-01" in fake_scheduler.jobs
    assert "system:goal-completion-advisor:analyst-01" in fake_scheduler.jobs
    assert "system:duty-drift:analyst-01" in fake_scheduler.jobs
    assert "system:duty-to-skill:analyst-01" in fake_scheduler.jobs
    assert "system:sharing-cycle:analyst-01" not in fake_scheduler.jobs
    job_args = fake_scheduler.jobs["goal:analyst-01:goal-1:health_check"].args
    assert job_args[5] is goal_inbox_store
    assert job_args[6] == "goal-1"
    assert job_args[7] == workspace_root
    assert heartbeat_runner.strategy is not None
    assert heartbeat_runner.strategy._config.goal_task_actions == frozenset({"recover"})
    assert heartbeat_runner.worker_router is worker_router
    assert heartbeat_runner.worker_scheduler is app.state.worker_schedulers["analyst-01"]
    assert heartbeat_runner.isolated_run_manager is isolated_run_manager
    assert isolated_run_manager._worker_schedulers is app.state.worker_schedulers
    assert channel_router.worker_router is worker_router
    assert channel_router.runtime_dependency_updates
    assert channel_router.runtime_dependency_updates[-1]["engine_dispatcher"] is engine_dispatcher
    assert "reload_order_approval" in approval_event_types()
    assert result["reload_metadata"]["trigger_source"] == "manual"
    assert result["reload_metadata"]["changed_files"] == []
    assert app.state.worker_reload_status[("demo", "analyst-01")]["trigger_source"] == "manual"


@pytest.mark.asyncio
async def test_reload_worker_runtime_records_auto_reload_reason(tmp_path: Path):
    workspace_root = tmp_path / "workspace"
    worker_dir = workspace_root / "tenants" / "demo" / "workers" / "analyst-01"
    (workspace_root / "system" / "skills").mkdir(parents=True, exist_ok=True)
    (workspace_root / "tenants" / "demo" / "skills").mkdir(parents=True, exist_ok=True)
    (worker_dir / "skills").mkdir(parents=True, exist_ok=True)
    persona_path = worker_dir / "PERSONA.md"
    _write_persona(
        persona_path,
        name="Analyst One",
        goal_task_action="escalate",
        include_monitor=False,
        include_contact=False,
    )

    entry = load_worker_entry(
        workspace_root=workspace_root,
        tenant_id="demo",
        worker_id="analyst-01",
    )
    registry = build_worker_registry([entry], default_worker_id="analyst-01")
    context = BootstrapContext(settings=SimpleNamespace(
        heartbeat_goal_task_actions="escalate,recover,investigate",
        heartbeat_goal_isolated_actions="replan,deep_review",
        heartbeat_goal_isolated_deviation_threshold=0.9,
    ))
    context.set_state("workspace_root", workspace_root)
    context.set_state("worker_registry", registry)
    context.set_state("worker_router", SimpleNamespace(_worker_registry=registry, _contact_registries={}))
    context.set_state("event_bus", EventBus())
    context.set_state("apscheduler", _FakeScheduler())
    context.set_state("trigger_managers", {})
    context.set_state("worker_schedulers", {})
    context.set_state("heartbeat_runners", {})
    context.set_state("contact_registries", {})
    context.set_state("sensor_registries", {})
    context.set_state("platform_client_factory", _StubPlatformClientFactory())

    app = FastAPI()
    _store_dependencies(app, context)

    result = await app.state.reload_worker_runtime(
        worker_id="analyst-01",
        tenant_id="demo",
        trigger_source="auto",
        changed_files=("rules/directives/dir-1.md", "goals/goal-1.md"),
    )

    assert result["reload_metadata"]["trigger_source"] == "auto"
    assert result["reload_metadata"]["changed_files"] == [
        "rules/directives/dir-1.md",
        "goals/goal-1.md",
    ]
    assert app.state.worker_reload_status[("demo", "analyst-01")]["trigger_source"] == "auto"


@pytest.mark.asyncio
async def test_reload_worker_runtime_uses_latest_app_state_worker_router(tmp_path: Path):
    workspace_root = tmp_path / "workspace"
    worker_dir = workspace_root / "tenants" / "demo" / "workers" / "analyst-01"
    (workspace_root / "system" / "skills").mkdir(parents=True, exist_ok=True)
    (workspace_root / "tenants" / "demo" / "skills").mkdir(parents=True, exist_ok=True)
    (worker_dir / "skills").mkdir(parents=True, exist_ok=True)
    persona_path = worker_dir / "PERSONA.md"
    _write_persona(
        persona_path,
        name="Analyst One",
        goal_task_action="escalate",
        include_monitor=False,
        include_contact=False,
    )

    entry = load_worker_entry(
        workspace_root=workspace_root,
        tenant_id="demo",
        worker_id="analyst-01",
    )
    registry = build_worker_registry([entry], default_worker_id="analyst-01")
    original_router = SimpleNamespace(_worker_registry=registry, _contact_registries={})
    replacement_router = _RouterWithRuntimeSetters(None)
    context = BootstrapContext(settings=SimpleNamespace(
        heartbeat_goal_task_actions="escalate,recover,investigate",
        heartbeat_goal_isolated_actions="replan,deep_review",
        heartbeat_goal_isolated_deviation_threshold=0.9,
    ))
    context.set_state("workspace_root", workspace_root)
    context.set_state("worker_registry", registry)
    context.set_state("worker_router", original_router)
    context.set_state("event_bus", EventBus())
    context.set_state("apscheduler", _FakeScheduler())
    context.set_state("trigger_managers", {})
    context.set_state("worker_schedulers", {})
    context.set_state("heartbeat_runners", {})
    context.set_state("contact_registries", {})
    context.set_state("sensor_registries", {})
    new_search_index = object()
    new_task_spawner = TaskSpawner(task_store=TaskStore(workspace_root), worker_schedulers={})
    context.set_state("session_search_index", new_search_index)
    context.set_state("task_spawner", new_task_spawner)

    app = FastAPI()
    _store_dependencies(app, context)
    app.state.worker_router = replacement_router

    await app.state.reload_worker_runtime(worker_id="analyst-01", tenant_id="demo")

    assert replacement_router._worker_registry is app.state.worker_registry
    assert replacement_router.session_search_index is new_search_index
    assert replacement_router.task_spawner is new_task_spawner
    assert app.state.session_search_index is new_search_index
    assert app.state.task_spawner is new_task_spawner


@pytest.mark.asyncio
async def test_reload_worker_runtime_skips_duplicate_duty_definitions(tmp_path: Path):
    workspace_root = tmp_path / "workspace"
    worker_dir = workspace_root / "tenants" / "demo" / "workers" / "analyst-01"
    (workspace_root / "system" / "skills").mkdir(parents=True, exist_ok=True)
    (workspace_root / "tenants" / "demo" / "skills").mkdir(parents=True, exist_ok=True)
    (worker_dir / "skills").mkdir(parents=True, exist_ok=True)
    (worker_dir / "duties").mkdir(parents=True, exist_ok=True)
    persona_path = worker_dir / "PERSONA.md"
    _write_persona(
        persona_path,
        name="Analyst One",
        goal_task_action="escalate",
        include_monitor=False,
        include_contact=False,
    )
    _write_event_duty(worker_dir / "duties" / "event-review-a.md", duty_id="event-review")
    _write_event_duty(worker_dir / "duties" / "event-review-b.md", duty_id="event-review")

    entry = load_worker_entry(
        workspace_root=workspace_root,
        tenant_id="demo",
        worker_id="analyst-01",
    )
    registry = build_worker_registry([entry], default_worker_id="analyst-01")
    event_bus = EventBus()
    context = BootstrapContext(settings=SimpleNamespace(
        heartbeat_goal_task_actions="escalate,recover,investigate",
        heartbeat_goal_isolated_actions="replan,deep_review",
        heartbeat_goal_isolated_deviation_threshold=0.9,
    ))
    context.set_state("workspace_root", workspace_root)
    context.set_state("worker_registry", registry)
    context.set_state("worker_router", SimpleNamespace(_worker_registry=registry, _contact_registries={}))
    context.set_state("event_bus", event_bus)
    context.set_state("apscheduler", _FakeScheduler())
    context.set_state("trigger_managers", {})
    context.set_state("worker_schedulers", {})
    context.set_state("heartbeat_runners", {})
    context.set_state("contact_registries", {})
    context.set_state("sensor_registries", {})
    context.set_state("platform_client_factory", _StubPlatformClientFactory())

    app = FastAPI()
    _store_dependencies(app, context)

    result = await app.state.reload_worker_runtime(
        worker_id="analyst-01",
        tenant_id="demo",
    )

    assert result["trigger_manager_refreshed"] is True
    assert event_bus.subscription_count == 1
    trigger_snapshot = app.state.trigger_managers["analyst-01"].registration_snapshot
    assert trigger_snapshot["duty_count"] == 1
    assert trigger_snapshot["resource_count"] == 1


@pytest.mark.asyncio
async def test_reload_worker_runtime_skips_duplicate_goal_definitions(tmp_path: Path):
    workspace_root = tmp_path / "workspace"
    worker_dir = workspace_root / "tenants" / "demo" / "workers" / "analyst-01"
    (workspace_root / "system" / "skills").mkdir(parents=True, exist_ok=True)
    (workspace_root / "tenants" / "demo" / "skills").mkdir(parents=True, exist_ok=True)
    (worker_dir / "skills").mkdir(parents=True, exist_ok=True)
    (worker_dir / "goals").mkdir(parents=True, exist_ok=True)
    persona_path = worker_dir / "PERSONA.md"
    _write_persona(
        persona_path,
        name="Analyst One",
        goal_task_action="escalate",
        include_monitor=False,
        include_contact=False,
    )
    first_goal = worker_dir / "goals" / "goal-a.md"
    second_goal = worker_dir / "goals" / "goal-b.md"
    _write_goal(first_goal, goal_id="goal-dup", title="First Goal")
    _write_goal(second_goal, goal_id="goal-dup", title="Second Goal")

    entry = load_worker_entry(
        workspace_root=workspace_root,
        tenant_id="demo",
        worker_id="analyst-01",
    )
    registry = build_worker_registry([entry], default_worker_id="analyst-01")
    fake_scheduler = _FakeScheduler()
    context = BootstrapContext(settings=SimpleNamespace(
        heartbeat_goal_task_actions="escalate,recover,investigate",
        heartbeat_goal_isolated_actions="replan,deep_review",
        heartbeat_goal_isolated_deviation_threshold=0.9,
    ))
    context.set_state("workspace_root", workspace_root)
    context.set_state("worker_registry", registry)
    context.set_state("worker_router", SimpleNamespace(_worker_registry=registry, _contact_registries={}))
    context.set_state("event_bus", EventBus())
    context.set_state("apscheduler", fake_scheduler)
    context.set_state("trigger_managers", {})
    context.set_state("worker_schedulers", {})
    context.set_state("heartbeat_runners", {})
    context.set_state("contact_registries", {})
    context.set_state("sensor_registries", {})
    context.set_state("platform_client_factory", _StubPlatformClientFactory())

    app = FastAPI()
    _store_dependencies(app, context)

    result = await app.state.reload_worker_runtime(
        worker_id="analyst-01",
        tenant_id="demo",
    )

    assert result["goal_checks_refreshed"] is True
    assert "goal:analyst-01:goal-dup:health_check" in fake_scheduler.jobs
    assert fake_scheduler.jobs["goal:analyst-01:goal-dup:health_check"].args[0] == first_goal


@pytest.mark.asyncio
async def test_reload_worker_runtime_refreshes_channel_bindings(tmp_path: Path):
    from src.bootstrap.channel_init import ChannelInitializer

    workspace_root = tmp_path / "workspace"
    worker_dir = workspace_root / "tenants" / "demo" / "workers" / "analyst-01"
    (workspace_root / "system" / "skills").mkdir(parents=True, exist_ok=True)
    (workspace_root / "tenants" / "demo" / "skills").mkdir(parents=True, exist_ok=True)
    (worker_dir / "skills").mkdir(parents=True, exist_ok=True)
    persona_path = worker_dir / "PERSONA.md"
    _write_persona(
        persona_path,
        name="Analyst One",
        goal_task_action="escalate",
        include_monitor=False,
        include_contact=False,
        channel_chat_id="oc_old",
    )

    entry = load_worker_entry(
        workspace_root=workspace_root,
        tenant_id="demo",
        worker_id="analyst-01",
    )
    registry = build_worker_registry([entry], default_worker_id="analyst-01")
    context = BootstrapContext(settings=SimpleNamespace(
        heartbeat_goal_task_actions="escalate,recover,investigate",
        heartbeat_goal_isolated_actions="replan,deep_review",
        heartbeat_goal_isolated_deviation_threshold=0.9,
    ))
    context.set_state("workspace_root", workspace_root)
    context.set_state("worker_registry", registry)
    context.set_state("worker_router", SimpleNamespace(_worker_registry=registry, _contact_registries={}))
    context.set_state("session_manager", SimpleNamespace())
    context.set_state("event_bus", EventBus())
    context.set_state("apscheduler", _FakeScheduler())
    context.set_state("trigger_managers", {})
    context.set_state("worker_schedulers", {})
    context.set_state("heartbeat_runners", {})
    context.set_state("contact_registries", {})
    context.set_state("sensor_registries", {})
    old_search_index = object()
    context.set_state("session_search_index", old_search_index)
    context.set_state(
        "task_spawner",
        TaskSpawner(
            task_store=TaskStore(workspace_root),
            worker_schedulers={},
            event_bus=context.get_state("event_bus"),
        ),
    )
    context.set_state(
        "platform_client_factory",
        _StubPlatformClientFactory({"feishu": SimpleNamespace()}),
    )

    await ChannelInitializer().initialize(context)

    app = FastAPI()
    _store_dependencies(app, context)

    assert app.state.im_channel_registry.find_by_chat_id("oc_old") is not None
    assert app.state.channel_message_router._session_search_index is old_search_index

    _write_persona(
        persona_path,
        name="Analyst One",
        goal_task_action="escalate",
        include_monitor=False,
        include_contact=False,
        channel_chat_id="oc_new",
    )
    new_search_index = object()
    context.set_state("session_search_index", new_search_index)

    result = await app.state.reload_worker_runtime(
        worker_id="analyst-01",
        tenant_id="demo",
    )

    assert result["channel_registry_refreshed"] is True
    assert app.state.im_channel_registry.find_by_chat_id("oc_old") is None
    assert app.state.im_channel_registry.find_by_chat_id("oc_new") is not None
    router = app.state.channel_message_router
    assert (
        router._contact_extractors["analyst-01"]._registry
        is app.state.contact_registries["analyst-01"]
    )
    assert router._session_search_index is new_search_index
    assert router._trigger_managers is app.state.trigger_managers
    assert router._worker_schedulers is app.state.worker_schedulers
    assert app.state.task_spawner._worker_schedulers is app.state.worker_schedulers
