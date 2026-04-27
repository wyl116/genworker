# edition: baseline
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from src.bootstrap.integration_init import IntegrationInitializer
from src.bootstrap.scheduler_init import SchedulerInitializer
from src.autonomy.inbox import SessionInboxStore
from src.events.bus import EventBus, Subscription
from src.events.models import Event
from src.worker.goal.models import ExternalSource, Goal, Milestone
from src.worker.integrations.goal_generator import write_goal_md
from src.worker.integrations.sync_manager import SyncManager
from src.worker.scripts.models import InlineScript


@dataclass
class MockWorkerScheduler:
    jobs: list[tuple[dict, int]] = field(default_factory=list)

    async def submit_task(self, job: dict, priority: int) -> bool:
        self.jobs.append((job, priority))
        return True


@dataclass
class RejectingWorkerScheduler:
    jobs: list[tuple[dict, int]] = field(default_factory=list)

    async def submit_task(self, job: dict, priority: int) -> bool:
        self.jobs.append((job, priority))
        return False


class MockChannelAdapter:
    def __init__(self) -> None:
        self.sent_messages = []

    async def send(self, message):
        self.sent_messages.append(message)
        return "msg-1"

    async def update_document(self, path: str, content: str, section=None) -> bool:
        return True


def _make_goal(**overrides) -> Goal:
    defaults = dict(
        goal_id="goal-health-1",
        title="Goal Health Check",
        status="active",
        priority="high",
        milestones=(
            Milestone(
                id="ms-1",
                title="Phase 1",
                status="pending",
                deadline="2000-01-01",
            ),
        ),
        external_source=ExternalSource(
            type="email",
            source_uri="email://inbox/goal-health",
            stakeholders=("owner@example.com",),
        ),
    )
    defaults.update(overrides)
    return Goal(**defaults)


class TestSchedulerGoalHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_direct_followup_sets_main_session_metadata(
        self,
        tmp_path,
        monkeypatch,
    ):
        init = SchedulerInitializer()
        worker_scheduler = MockWorkerScheduler()
        event_bus = EventBus()
        monkeypatch.setattr(
            "src.worker.lifecycle.detectors.resolve_gate_level",
            lambda **kwargs: "auto",
        )
        goal_path = write_goal_md(
            _make_goal(external_source=None),
            tmp_path,
            filename="goal.md",
        )

        await init._run_goal_health_check(
            goal_path,
            "test",
            "worker-1",
            worker_scheduler,
            event_bus,
            None,
        )

        assert len(worker_scheduler.jobs) == 1
        job, priority = worker_scheduler.jobs[0]
        assert priority > 0
        assert job["main_session_key"] == "main:worker-1"
        assert job["thread_id"] == "main:worker-1"
        assert job["manifest"].main_session_key == "main:worker-1"

    @pytest.mark.asyncio
    async def test_health_check_direct_followup_copies_goal_default_pre_script(
        self,
        tmp_path,
        monkeypatch,
    ):
        init = SchedulerInitializer()
        worker_scheduler = MockWorkerScheduler()
        event_bus = EventBus()
        monkeypatch.setattr(
            "src.worker.lifecycle.detectors.resolve_gate_level",
            lambda **kwargs: "auto",
        )
        goal_path = write_goal_md(
            _make_goal(
                external_source=None,
                default_pre_script=InlineScript(source="print('goal prefetch')"),
            ),
            tmp_path,
            filename="goal-script.md",
        )

        await init._run_goal_health_check(
            goal_path,
            "test",
            "worker-1",
            worker_scheduler,
            event_bus,
            None,
        )

        assert len(worker_scheduler.jobs) == 1
        manifest = worker_scheduler.jobs[0][0]["manifest"]
        assert isinstance(manifest.pre_script, InlineScript)
        assert manifest.pre_script.source.strip() == "print('goal prefetch')"

    @pytest.mark.asyncio
    async def test_health_check_direct_followup_publishes_failed_event_on_quota_rejection(
        self,
        tmp_path,
        monkeypatch,
    ):
        init = SchedulerInitializer()
        worker_scheduler = RejectingWorkerScheduler()
        event_bus = EventBus()
        failed_events: list[dict[str, object]] = []

        async def _capture_failed(event: Event) -> None:
            failed_events.append(dict(event.payload))

        event_bus.subscribe(Subscription(
            handler_id="goal-health-failed",
            event_type="task.failed",
            tenant_id="test",
            handler=_capture_failed,
        ))
        monkeypatch.setattr(
            "src.worker.lifecycle.detectors.resolve_gate_level",
            lambda **kwargs: "auto",
        )
        goal_path = write_goal_md(
            _make_goal(external_source=None),
            tmp_path,
            filename="goal.md",
        )

        await init._run_goal_health_check(
            goal_path,
            "test",
            "worker-1",
            worker_scheduler,
            event_bus,
            None,
        )

        assert len(worker_scheduler.jobs) == 1
        assert failed_events
        assert failed_events[0]["thread_id"] == "main:worker-1"
        assert failed_events[0]["error_code"] == "QUOTA_EXHAUSTED"

    @pytest.mark.asyncio
    async def test_health_check_enqueues_task_and_publishes_followup(self, tmp_path):
        init = SchedulerInitializer()
        worker_scheduler = MockWorkerScheduler()
        event_bus = EventBus()
        inbox_store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
        published_events: list[Event] = []

        async def _capture(event: Event) -> None:
            published_events.append(event)

        event_bus.subscribe(Subscription(
            handler_id="goal-health-capture",
            event_type="goal.progress_update_requested",
            tenant_id="test",
            handler=_capture,
        ))

        goal_path = write_goal_md(_make_goal(), tmp_path, filename="goal.md")

        await init._run_goal_health_check(
            goal_path,
            "test",
            "worker-1",
            worker_scheduler,
            event_bus,
            inbox_store,
        )

        assert len(worker_scheduler.jobs) == 0
        pending = await inbox_store.fetch_pending(tenant_id="test", worker_id="worker-1")
        assert len(pending) == 1
        assert pending[0].event_type == "goal.health_check_detected"

    @pytest.mark.asyncio
    async def test_health_check_inbox_payload_preserves_goal_default_pre_script(self, tmp_path):
        init = SchedulerInitializer()
        worker_scheduler = MockWorkerScheduler()
        event_bus = EventBus()
        inbox_store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
        goal_path = write_goal_md(
            _make_goal(
                default_pre_script=InlineScript(source="print('goal inbox prefetch')"),
            ),
            tmp_path,
            filename="goal-inbox-script.md",
        )

        await init._run_goal_health_check(
            goal_path,
            "test",
            "worker-1",
            worker_scheduler,
            event_bus,
            inbox_store,
        )

        pending = await inbox_store.fetch_pending(tenant_id="test", worker_id="worker-1")
        assert len(pending) == 1
        assert pending[0].payload["pre_script"]["kind"] == "inline"
        assert pending[0].payload["pre_script"]["source"].strip() == "print('goal inbox prefetch')"
        assert pending[0].payload["goal_id"] == "goal-health-1"

    @pytest.mark.asyncio
    async def test_health_check_skips_followup_for_proceeding_goal(self, tmp_path):
        init = SchedulerInitializer()
        worker_scheduler = MockWorkerScheduler()
        event_bus = EventBus()
        inbox_store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
        published_events: list[Event] = []

        async def _capture(event: Event) -> None:
            published_events.append(event)

        event_bus.subscribe(Subscription(
            handler_id="goal-health-capture",
            event_type="goal.progress_update_requested",
            tenant_id="test",
            handler=_capture,
        ))

        goal = _make_goal(
            priority="medium",
            milestones=(
                Milestone(id="ms-1", title="Done", status="completed"),
            ),
        )
        goal_path = write_goal_md(goal, tmp_path, filename="goal.md")

        await init._run_goal_health_check(
            goal_path,
            "test",
            "worker-1",
            worker_scheduler,
            event_bus,
            inbox_store,
        )

        assert worker_scheduler.jobs == []
        assert published_events == []
        pending = await inbox_store.fetch_pending(tenant_id="test", worker_id="worker-1")
        assert pending == ()

    @pytest.mark.asyncio
    async def test_health_check_recovers_when_goal_file_path_is_stale(self, tmp_path):
        init = SchedulerInitializer()
        worker_scheduler = MockWorkerScheduler()
        event_bus = EventBus()
        inbox_store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
        published_events: list[Event] = []

        async def _capture(event: Event) -> None:
            published_events.append(event)

        event_bus.subscribe(Subscription(
            handler_id="goal-health-capture",
            event_type="goal.progress_update_requested",
            tenant_id="test",
            handler=_capture,
        ))

        worker_goals_dir = tmp_path / "tenants" / "test" / "workers" / "worker-1" / "goals"
        goal_path = write_goal_md(_make_goal(), worker_goals_dir, filename="renamed-goal.md")
        stale_path = worker_goals_dir / "missing-goal.md"

        await init._run_goal_health_check(
            stale_path,
            "test",
            "worker-1",
            worker_scheduler,
            event_bus,
            inbox_store,
            goal_id="goal-health-1",
            workspace_root=tmp_path,
        )

        assert goal_path.exists()
        pending = await inbox_store.fetch_pending(tenant_id="test", worker_id="worker-1")
        assert len(pending) == 1
        assert pending[0].payload["goal_file"] == str(goal_path)
        assert len(published_events) == 1
        payload = dict(published_events[0].payload)
        assert payload["goal_file"] == str(goal_path)

    @pytest.mark.asyncio
    async def test_health_check_gated_followup_creates_confirmation_when_inbox_missing(
        self,
        tmp_path,
        monkeypatch,
    ):
        init = SchedulerInitializer()
        worker_scheduler = MockWorkerScheduler()
        event_bus = EventBus()
        goal_path = write_goal_md(_make_goal(), tmp_path, filename="goal.md")

        monkeypatch.setattr(
            "src.worker.lifecycle.detectors.resolve_gate_level",
            lambda **kwargs: "gated",
        )

        await init._run_goal_health_check(
            goal_path,
            "test",
            "worker-1",
            worker_scheduler,
            event_bus,
            None,
            goal_id="goal-health-1",
            workspace_root=tmp_path,
        )

        assert worker_scheduler.jobs == []
        confirmation_store = SessionInboxStore(
            redis_client=None,
            fallback_dir=tmp_path,
            event_bus=event_bus,
        )
        confirmations = await confirmation_store.list_pending(
            tenant_id="test",
            worker_id="worker-1",
            event_type="task.confirmation_requested",
            limit=20,
        )
        assert len(confirmations) == 1
        payload = confirmations[0].payload
        assert payload["task_kind"] == "goal_followup"
        assert payload["goal_id"] == "goal-health-1"
        assert payload["manifest"]["provenance"]["source_type"] == "goal_followup"


class TestIntegrationGoalSyncSubscription:
    @pytest.mark.asyncio
    async def test_progress_request_event_triggers_sync_manager(self, tmp_path):
        event_bus = EventBus()
        adapter = MockChannelAdapter()
        sync_manager = SyncManager(adapter)
        init = IntegrationInitializer()
        init._register_goal_sync_subscriptions(
            event_bus=event_bus,
            tenant_id="test",
            sync_manager=sync_manager,
        )

        goal_path = write_goal_md(_make_goal(), tmp_path, filename="goal.md")

        event = Event(
            event_id="evt-1",
            type="goal.progress_update_requested",
            source="test",
            tenant_id="test",
            payload=(
                ("goal_file", str(goal_path)),
                ("goal_id", "goal-health-1"),
                ("worker_id", "worker-1"),
            ),
        )
        count = await event_bus.publish(event)

        assert count == 1
        assert len(adapter.sent_messages) == 1
        assert adapter.sent_messages[0].message_type == "progress_inquiry"

    @pytest.mark.asyncio
    async def test_progress_request_recovers_when_goal_file_path_is_stale(self, tmp_path):
        event_bus = EventBus()
        adapter = MockChannelAdapter()
        sync_manager = SyncManager(adapter)
        init = IntegrationInitializer()
        init._register_goal_sync_subscriptions(
            event_bus=event_bus,
            tenant_id="test",
            sync_manager=sync_manager,
            workspace_root=tmp_path,
        )

        worker_goals_dir = tmp_path / "tenants" / "test" / "workers" / "worker-1" / "goals"
        goal_path = write_goal_md(_make_goal(), worker_goals_dir, filename="renamed-goal.md")
        stale_path = worker_goals_dir / "missing-goal.md"

        event = Event(
            event_id="evt-stale-1",
            type="goal.progress_update_requested",
            source="test",
            tenant_id="test",
            payload=(
                ("goal_file", str(stale_path)),
                ("goal_id", "goal-health-1"),
                ("worker_id", "worker-1"),
            ),
        )
        count = await event_bus.publish(event)

        assert count == 1
        assert goal_path.exists()
        assert len(adapter.sent_messages) == 1
        assert adapter.sent_messages[0].message_type == "progress_inquiry"
