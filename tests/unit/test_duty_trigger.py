# edition: baseline
"""
Tests for TriggerManager and trigger registration.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.events.bus import EventBus
from src.events.models import Event
from src.worker.duty.models import (
    Duty,
    DutyTrigger,
    ExecutionPolicy,
)
from src.worker.duty.trigger_manager import (
    TriggerManager,
    _parse_interval_to_seconds,
    select_execution_depth,
)


# --- select_execution_depth tests ---

class TestSelectExecutionDepth:
    def test_default_depth(self):
        duty = Duty(
            duty_id="d1", title="Test", status="active",
            triggers=(), execution_policy=ExecutionPolicy(default="standard"),
            action="do", quality_criteria=("ok",),
        )
        assert select_execution_depth(duty, "any-trigger") == "standard"

    def test_override_depth(self):
        duty = Duty(
            duty_id="d1", title="Test", status="active",
            triggers=(), execution_policy=ExecutionPolicy(
                default="standard",
                overrides=(("t1", "deep"), ("t2", "quick")),
            ),
            action="do", quality_criteria=("ok",),
        )
        assert select_execution_depth(duty, "t1") == "deep"
        assert select_execution_depth(duty, "t2") == "quick"
        assert select_execution_depth(duty, "t3") == "standard"


# --- _parse_interval_to_seconds tests ---

class TestParseInterval:
    def test_minutes(self):
        assert _parse_interval_to_seconds("5m") == 300

    def test_hours(self):
        assert _parse_interval_to_seconds("1h") == 3600

    def test_seconds(self):
        assert _parse_interval_to_seconds("30s") == 30

    def test_10_minutes(self):
        assert _parse_interval_to_seconds("10m") == 600

    def test_combined(self):
        assert _parse_interval_to_seconds("1h30m") == 5400

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Invalid interval"):
            _parse_interval_to_seconds("invalid")


# --- TriggerManager tests ---

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
def mock_executor():
    executor = AsyncMock()
    executor.execute = AsyncMock()
    return executor


def _make_duty(triggers: tuple[DutyTrigger, ...]) -> Duty:
    return Duty(
        duty_id="test-duty",
        title="Test Duty",
        status="active",
        triggers=triggers,
        execution_policy=ExecutionPolicy(),
        action="Test action",
        quality_criteria=("passes",),
    )


class TestTriggerManagerRegister:
    @pytest.mark.asyncio
    async def test_register_schedule_trigger(
        self, mock_scheduler, event_bus, mock_executor,
    ):
        mgr = TriggerManager(mock_scheduler, event_bus, mock_executor)
        trigger = DutyTrigger(id="sched-1", type="schedule", cron="0 9 * * *")
        duty = _make_duty((trigger,))

        await mgr.register_duty(duty, "t1", "w1")

        assert mock_scheduler.add_job.called
        assert "test-duty" in mgr.registered_duties

    @pytest.mark.asyncio
    async def test_register_event_trigger(
        self, mock_scheduler, event_bus, mock_executor,
    ):
        mgr = TriggerManager(mock_scheduler, event_bus, mock_executor)
        trigger = DutyTrigger(
            id="evt-1", type="event", source="data.file_uploaded",
        )
        duty = _make_duty((trigger,))

        await mgr.register_duty(duty, "t1", "w1")

        assert event_bus.subscription_count == 1
        assert "test-duty" in mgr.registered_duties

    @pytest.mark.asyncio
    async def test_register_condition_trigger(
        self, mock_scheduler, event_bus, mock_executor,
    ):
        mgr = TriggerManager(mock_scheduler, event_bus, mock_executor)
        trigger = DutyTrigger(
            id="cond-1", type="condition",
            metric="error_rate", rule="> 0.1",
            check_interval="10m",
        )
        duty = _make_duty((trigger,))

        await mgr.register_duty(duty, "t1", "w1")

        # Verify APScheduler was called for interval job
        call_args = mock_scheduler.add_job.call_args
        assert call_args is not None
        # The interval trigger should have seconds=600 (10m)
        interval_trigger = call_args.kwargs.get("trigger") or call_args[1].get("trigger")
        assert interval_trigger is not None

    @pytest.mark.asyncio
    async def test_register_manual_trigger(
        self, mock_scheduler, event_bus, mock_executor,
    ):
        mgr = TriggerManager(mock_scheduler, event_bus, mock_executor)
        trigger = DutyTrigger(id="man-1", type="manual")
        duty = _make_duty((trigger,))

        await mgr.register_duty(duty, "t1", "w1")

        # Manual triggers don't create scheduler jobs or subscriptions
        assert mock_scheduler.add_job.call_count == 0
        assert event_bus.subscription_count == 0
        assert "test-duty" in mgr.registered_duties

    @pytest.mark.asyncio
    async def test_register_collaboration_trigger(
        self, mock_scheduler, event_bus, mock_executor,
    ):
        mgr = TriggerManager(mock_scheduler, event_bus, mock_executor)
        trigger = DutyTrigger(id="collab-1", type="collaboration")
        duty = _make_duty((trigger,))

        await mgr.register_duty(duty, "t1", "w1")
        assert "test-duty" in mgr.registered_duties

    @pytest.mark.asyncio
    async def test_register_multiple_triggers(
        self, mock_scheduler, event_bus, mock_executor,
    ):
        mgr = TriggerManager(mock_scheduler, event_bus, mock_executor)
        triggers = (
            DutyTrigger(id="s1", type="schedule", cron="0 9 * * *"),
            DutyTrigger(id="e1", type="event", source="data.*"),
            DutyTrigger(id="m1", type="manual"),
        )
        duty = _make_duty(triggers)

        await mgr.register_duty(duty, "t1", "w1")
        assert "test-duty" in mgr.registered_duties

    @pytest.mark.asyncio
    async def test_unregister_duty(
        self, mock_scheduler, event_bus, mock_executor,
    ):
        mgr = TriggerManager(mock_scheduler, event_bus, mock_executor)
        trigger = DutyTrigger(id="s1", type="schedule", cron="0 9 * * *")
        duty = _make_duty((trigger,))

        await mgr.register_duty(duty, "t1", "w1")
        assert "test-duty" in mgr.registered_duties

        await mgr.unregister_duty("test-duty")
        assert "test-duty" not in mgr.registered_duties
        assert mock_scheduler.remove_job.called

    @pytest.mark.asyncio
    async def test_unregister_event_trigger_removes_subscription(
        self, mock_scheduler, event_bus, mock_executor,
    ):
        mgr = TriggerManager(mock_scheduler, event_bus, mock_executor)
        duty = _make_duty((
            DutyTrigger(id="evt-1", type="event", source="data.file_uploaded"),
        ))

        await mgr.register_duty(duty, "t1", "w1")
        await mgr.unregister_duty("test-duty")

        event = Event(
            event_id="e1",
            type="data.file_uploaded",
            source="test",
            tenant_id="t1",
        )
        count = await event_bus.publish(event)
        assert count == 0


class TestTriggerManagerEventFiring:
    @pytest.mark.asyncio
    async def test_event_trigger_fires_on_matching_event(
        self, mock_scheduler, event_bus, mock_executor,
    ):
        mgr = TriggerManager(mock_scheduler, event_bus, mock_executor)
        trigger = DutyTrigger(
            id="evt-1", type="event", source="data.file_uploaded",
        )
        duty = _make_duty((trigger,))

        await mgr.register_duty(duty, "t1", "w1")

        # Publish a matching event
        event = Event(
            event_id="e1", type="data.file_uploaded",
            source="test", tenant_id="t1",
        )
        count = await event_bus.publish(event)
        assert count == 1
        assert mock_executor.execute.called

    @pytest.mark.asyncio
    async def test_event_trigger_ignores_non_matching_event(
        self, mock_scheduler, event_bus, mock_executor,
    ):
        mgr = TriggerManager(mock_scheduler, event_bus, mock_executor)
        trigger = DutyTrigger(
            id="evt-1", type="event", source="data.file_uploaded",
        )
        duty = _make_duty((trigger,))

        await mgr.register_duty(duty, "t1", "w1")

        # Publish a non-matching event
        event = Event(
            event_id="e1", type="user.created",
            source="test", tenant_id="t1",
        )
        count = await event_bus.publish(event)
        assert count == 0
        assert not mock_executor.execute.called


class TestTriggerManagerConditionChecks:
    @pytest.mark.asyncio
    async def test_condition_trigger_fires_when_rule_matches(
        self, mock_scheduler, event_bus, mock_executor,
    ):
        mgr = TriggerManager(
            mock_scheduler,
            event_bus,
            mock_executor,
            metric_provider=lambda duty, trigger: 0.25,
        )
        trigger = DutyTrigger(
            id="cond-1",
            type="condition",
            metric="error_rate",
            rule="> 0.1",
            check_interval="10m",
        )
        duty = _make_duty((trigger,))

        await mgr._check_condition(duty, trigger, "t1", "w1")

        assert mock_executor.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_condition_trigger_skips_when_rule_not_matched(
        self, mock_scheduler, event_bus, mock_executor,
    ):
        mgr = TriggerManager(
            mock_scheduler,
            event_bus,
            mock_executor,
            metric_provider=lambda duty, trigger: 0.01,
        )
        trigger = DutyTrigger(
            id="cond-1",
            type="condition",
            metric="error_rate",
            rule="> 0.1",
            check_interval="10m",
        )
        duty = _make_duty((trigger,))

        await mgr._check_condition(duty, trigger, "t1", "w1")

        assert mock_executor.execute.await_count == 0

    @pytest.mark.asyncio
    async def test_condition_trigger_skips_invalid_rule(
        self, mock_scheduler, event_bus, mock_executor,
    ):
        mgr = TriggerManager(
            mock_scheduler,
            event_bus,
            mock_executor,
            metric_provider=lambda duty, trigger: 0.5,
        )
        trigger = DutyTrigger(
            id="cond-1",
            type="condition",
            metric="error_rate",
            rule="unexpected",
            check_interval="10m",
        )
        duty = _make_duty((trigger,))

        await mgr._check_condition(duty, trigger, "t1", "w1")

        assert mock_executor.execute.await_count == 0
