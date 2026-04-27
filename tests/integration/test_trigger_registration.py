# edition: baseline
"""
Integration tests for trigger registration end-to-end flow.

Tests the full chain: parse DUTY.md -> register triggers -> fire events -> execute.
"""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.events.bus import EventBus
from src.events.models import Event
from src.worker.duty.duty_executor import DutyExecutor
from src.worker.duty.models import Duty, DutyTrigger, ExecutionPolicy
from src.worker.duty.parser import parse_duty
from src.worker.duty.trigger_manager import TriggerManager
from src.worker.scheduler import SchedulerConfig, TriggerPriority, WorkerScheduler


FULL_DUTY_MD = """---
duty_id: integration-test-duty
title: Integration Test Duty
status: active
triggers:
  - id: daily-schedule
    type: schedule
    cron: "0 9 * * *"
    description: Daily check
  - id: on-data-upload
    type: event
    source: data.file_uploaded
    description: On file upload
    filter:
      type: csv
  - id: error-rate-check
    type: condition
    metric: error_rate
    rule: "> 0.1"
    check_interval: "10m"
    description: Error rate monitor
  - id: manual-run
    type: manual
    description: Manual execution
execution_policy:
  default: standard
  overrides:
    daily-schedule: deep
    error-rate-check: quick
quality_criteria:
  - All data sources checked
  - No critical errors
---

Perform comprehensive data quality check across all sources.
"""


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def mock_scheduler():
    scheduler = MagicMock()
    scheduler.add_job = MagicMock()
    scheduler.remove_job = MagicMock()
    return scheduler


@pytest.fixture
def mock_router():
    router = AsyncMock()

    async def fake_stream(*args, **kwargs):
        yield type("Event", (), {"content": "completed", "run_id": "r1"})()

    router.route_stream = fake_stream
    return router


class TestFullRegistrationFlow:
    @pytest.mark.asyncio
    async def test_parse_and_register_all_triggers(
        self, mock_scheduler, event_bus, mock_router, tmp_path,
    ):
        duty = parse_duty(FULL_DUTY_MD)
        assert duty.duty_id == "integration-test-duty"
        assert len(duty.triggers) == 4

        executor = DutyExecutor(mock_router, tmp_path)
        mgr = TriggerManager(mock_scheduler, event_bus, executor)

        await mgr.register_duty(duty, "tenant-1", "worker-1")

        # Verify: 1 schedule + 1 condition = 2 APScheduler jobs
        assert mock_scheduler.add_job.call_count == 2

        # Verify: 1 event subscription
        assert event_bus.subscription_count == 1

        # Verify registered
        assert "integration-test-duty" in mgr.registered_duties

    @pytest.mark.asyncio
    async def test_event_trigger_fires_duty_execution(
        self, mock_scheduler, event_bus, mock_router, tmp_path,
    ):
        duty = parse_duty(FULL_DUTY_MD)
        executor = DutyExecutor(mock_router, tmp_path)
        mgr = TriggerManager(mock_scheduler, event_bus, executor)

        await mgr.register_duty(duty, "tenant-1", "worker-1")

        # Publish matching event (type=csv matches filter)
        event = Event(
            event_id="e1",
            type="data.file_uploaded",
            source="upload-service",
            tenant_id="tenant-1",
            payload=(("type", "csv"), ("filename", "data.csv")),
        )
        count = await event_bus.publish(event)
        assert count == 1

    @pytest.mark.asyncio
    async def test_event_filter_rejects_non_matching(
        self, mock_scheduler, event_bus, mock_router, tmp_path,
    ):
        duty = parse_duty(FULL_DUTY_MD)
        executor = DutyExecutor(mock_router, tmp_path)
        mgr = TriggerManager(mock_scheduler, event_bus, executor)

        await mgr.register_duty(duty, "tenant-1", "worker-1")

        # Publish event with wrong filter value
        event = Event(
            event_id="e2",
            type="data.file_uploaded",
            source="upload-service",
            tenant_id="tenant-1",
            payload=(("type", "json"),),
        )
        count = await event_bus.publish(event)
        assert count == 0

    @pytest.mark.asyncio
    async def test_event_filter_supports_regex_matching(
        self, mock_scheduler, event_bus, mock_router, tmp_path,
    ):
        duty_md = """---
duty_id: regex-duty
title: Regex Duty
status: active
triggers:
  - id: on-report-upload
    type: event
    source: data.file_uploaded
    filter:
      filename: "regex:^report-\\\\d+\\\\.csv$"
quality_criteria:
  - Matched report upload
---

Handle the uploaded report.
"""
        duty = parse_duty(duty_md)
        executor = DutyExecutor(mock_router, tmp_path)
        mgr = TriggerManager(mock_scheduler, event_bus, executor)

        await mgr.register_duty(duty, "tenant-1", "worker-1")

        event = Event(
            event_id="e-regex-1",
            type="data.file_uploaded",
            source="upload-service",
            tenant_id="tenant-1",
            payload=(("filename", "report-42.csv"),),
        )
        count = await event_bus.publish(event)
        assert count == 1

    @pytest.mark.asyncio
    async def test_tenant_isolation_in_triggers(
        self, mock_scheduler, event_bus, mock_router, tmp_path,
    ):
        duty = parse_duty(FULL_DUTY_MD)
        executor = DutyExecutor(mock_router, tmp_path)
        mgr = TriggerManager(mock_scheduler, event_bus, executor)

        await mgr.register_duty(duty, "tenant-1", "worker-1")

        # Event for different tenant should not trigger
        event = Event(
            event_id="e3",
            type="data.file_uploaded",
            source="test",
            tenant_id="tenant-2",
            payload=(("type", "csv"),),
        )
        count = await event_bus.publish(event)
        assert count == 0

    @pytest.mark.asyncio
    async def test_unregister_cleans_up(
        self, mock_scheduler, event_bus, mock_router, tmp_path,
    ):
        duty = parse_duty(FULL_DUTY_MD)
        executor = DutyExecutor(mock_router, tmp_path)
        mgr = TriggerManager(mock_scheduler, event_bus, executor)

        await mgr.register_duty(duty, "tenant-1", "worker-1")
        assert "integration-test-duty" in mgr.registered_duties

        await mgr.unregister_duty("integration-test-duty")
        assert "integration-test-duty" not in mgr.registered_duties

    @pytest.mark.asyncio
    async def test_execution_depth_per_trigger(
        self, mock_scheduler, event_bus, mock_router, tmp_path,
    ):
        duty = parse_duty(FULL_DUTY_MD)

        # Verify depths
        assert duty.depth_for_trigger("daily-schedule") == "deep"
        assert duty.depth_for_trigger("error-rate-check") == "quick"
        assert duty.depth_for_trigger("on-data-upload") == "standard"
        assert duty.depth_for_trigger("manual-run") == "standard"


class TestSchedulerPriority:
    def test_duty_priority_over_goal(self):
        p = TriggerPriority()
        assert p.DUTY < p.GOAL

    def test_duty_priority_over_persona_trigger(self):
        p = TriggerPriority()
        assert p.DUTY < p.PERSONA_TRIGGER

    @pytest.mark.asyncio
    async def test_scheduler_accepts_tasks_within_quota(self):
        config = SchedulerConfig(max_concurrent_tasks=5, daily_task_quota=10)
        scheduler = WorkerScheduler(config=config)

        result = await scheduler.submit_task(
            {"task": "test", "tenant_id": "t1"},
            priority=TriggerPriority().DUTY,
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_scheduler_rejects_over_quota(self):
        config = SchedulerConfig(max_concurrent_tasks=5, daily_task_quota=2)
        scheduler = WorkerScheduler(config=config)

        await scheduler.submit_task(
            {"task": "t1", "tenant_id": "t1"}, priority=10,
        )
        await scheduler.submit_task(
            {"task": "t2", "tenant_id": "t1"}, priority=10,
        )

        result = await scheduler.submit_task(
            {"task": "t3", "tenant_id": "t1"}, priority=10,
        )
        assert result is False


class TestConditionCheckInterval:
    @pytest.mark.asyncio
    async def test_10m_interval_registers_correctly(
        self, mock_scheduler, event_bus, mock_router, tmp_path,
    ):
        """Verify check_interval='10m' results in 600 second interval."""
        duty = parse_duty(FULL_DUTY_MD)
        executor = DutyExecutor(mock_router, tmp_path)
        mgr = TriggerManager(mock_scheduler, event_bus, executor)

        await mgr.register_duty(duty, "tenant-1", "worker-1")

        # Find the condition job registration call by job ID pattern
        condition_calls = [
            call for call in mock_scheduler.add_job.call_args_list
            if call.kwargs.get("id", "").startswith("duty:") and ":condition:" in call.kwargs.get("id", "")
        ]
        assert len(condition_calls) == 1

        # Verify the interval trigger was created with 600 seconds
        call_kwargs = condition_calls[0].kwargs
        interval_trigger = call_kwargs.get("trigger")
        assert interval_trigger is not None
        # IntervalTrigger should have interval of 600 seconds
        assert interval_trigger.interval.total_seconds() == 600
