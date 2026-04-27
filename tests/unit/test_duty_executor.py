# edition: baseline
"""
Tests for DutyExecutor and build_duty_prompt.
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.worker.duty.duty_executor import DutyExecutor, build_duty_prompt
from src.worker.duty.execution_log import load_recent_records, write_execution_record
from src.worker.duty.models import (
    Duty,
    DutyExecutionRecord,
    DutyTrigger,
    EscalationPolicy,
    ExecutionPolicy,
)
from src.worker.scripts.models import InlineScript


# --- build_duty_prompt tests ---

def _make_duty(
    depth_default: str = "standard",
    overrides: tuple = (),
    escalation: EscalationPolicy | None = None,
) -> Duty:
    return Duty(
        duty_id="test-duty",
        title="Data Quality Check",
        status="active",
        triggers=(
            DutyTrigger(id="t1", type="schedule", cron="0 9 * * *"),
        ),
        execution_policy=ExecutionPolicy(
            default=depth_default, overrides=overrides,
        ),
        action="Check all data sources for quality issues.",
        quality_criteria=(
            "All fields must have values",
            "Formats match schema",
        ),
        escalation=escalation,
    )


class TestBuildDutyPrompt:
    def test_standard_prompt_contains_key_elements(self):
        duty = _make_duty()
        trigger = duty.triggers[0]
        prompt = build_duty_prompt(duty, "standard", trigger)

        assert "[Duty Execution] Data Quality Check" in prompt
        assert "Trigger: schedule" in prompt
        assert "Execution Depth: standard" in prompt
        assert "Check all data sources" in prompt
        assert "All fields must have values" in prompt
        assert "Formats match schema" in prompt

    def test_deep_mode_includes_root_cause(self):
        duty = _make_duty()
        trigger = duty.triggers[0]
        prompt = build_duty_prompt(duty, "deep", trigger)

        assert "Root Cause Analysis Requirements" in prompt
        assert "root cause" in prompt.lower()
        assert "preventive measures" in prompt.lower()

    def test_standard_mode_no_root_cause(self):
        duty = _make_duty()
        trigger = duty.triggers[0]
        prompt = build_duty_prompt(duty, "standard", trigger)

        assert "Root Cause Analysis" not in prompt

    def test_quick_mode_no_root_cause(self):
        duty = _make_duty()
        trigger = duty.triggers[0]
        prompt = build_duty_prompt(duty, "quick", trigger)

        assert "Root Cause Analysis" not in prompt

    def test_prompt_includes_quality_criteria_numbered(self):
        duty = _make_duty()
        trigger = duty.triggers[0]
        prompt = build_duty_prompt(duty, "standard", trigger)

        assert "1. All fields must have values" in prompt
        assert "2. Formats match schema" in prompt


# --- ExecutionLog tests ---

class TestExecutionLog:
    def test_write_and_load_records(self, tmp_path):
        duty_dir = tmp_path / "test-duty"
        record = DutyExecutionRecord(
            execution_id="exec-001",
            duty_id="test-duty",
            trigger_id="t1",
            depth="standard",
            executed_at="2026-01-01T00:00:00Z",
            duration_seconds=1.5,
            conclusion="completed successfully",
            anomalies_found=("anomaly-1",),
            escalated=False,
            task_id="task-001",
        )

        write_execution_record(duty_dir, record)
        records = load_recent_records(duty_dir)

        assert len(records) == 1
        assert records[0].execution_id == "exec-001"
        assert records[0].anomalies_found == ("anomaly-1",)
        assert records[0].duration_seconds == 1.5

    def test_load_recent_with_limit(self, tmp_path):
        duty_dir = tmp_path / "test-duty"

        for i in range(15):
            record = DutyExecutionRecord(
                execution_id=f"exec-{i:03d}",
                duty_id="test-duty",
                trigger_id="t1",
                depth="standard",
                executed_at=f"2026-01-{i+1:02d}T00:00:00Z",
                duration_seconds=float(i),
                conclusion=f"run {i}",
            )
            write_execution_record(duty_dir, record)

        records = load_recent_records(duty_dir, limit=5)
        assert len(records) == 5
        # Should be the last 5 records
        assert records[0].execution_id == "exec-010"
        assert records[4].execution_id == "exec-014"

    def test_load_empty_directory(self, tmp_path):
        duty_dir = tmp_path / "nonexistent"
        records = load_recent_records(duty_dir)
        assert records == ()

    def test_append_multiple_records(self, tmp_path):
        duty_dir = tmp_path / "test-duty"

        for i in range(3):
            record = DutyExecutionRecord(
                execution_id=f"exec-{i}",
                duty_id="test-duty",
                trigger_id="t1",
                depth="standard",
                executed_at="2026-01-01T00:00:00Z",
                duration_seconds=1.0,
                conclusion=f"run {i}",
            )
            write_execution_record(duty_dir, record)

        records = load_recent_records(duty_dir)
        assert len(records) == 3


# --- DutyExecutor tests ---

class TestDutyExecutor:
    @pytest.mark.asyncio
    async def test_execute_writes_log(self, tmp_path):
        mock_router = AsyncMock()

        async def fake_stream(*args, **kwargs):
            yield type("Event", (), {"content": "done", "run_id": "r1"})()

        mock_router.route_stream = fake_stream

        executor = DutyExecutor(
            worker_router=mock_router,
            execution_log_dir=tmp_path,
        )

        duty = _make_duty()
        trigger = duty.triggers[0]

        record = await executor.execute(duty, trigger, "t1", "w1")

        assert record.duty_id == "test-duty"
        assert record.trigger_id == "t1"
        assert record.depth == "standard"
        assert record.duration_seconds >= 0

        # Verify log was written
        records = load_recent_records(tmp_path / "test-duty")
        assert len(records) == 1

    @pytest.mark.asyncio
    async def test_execute_handles_error(self, tmp_path):
        mock_router = AsyncMock()

        async def fail_stream(*args, **kwargs):
            raise RuntimeError("engine failed")
            yield  # Make it a generator

        mock_router.route_stream = fail_stream

        executor = DutyExecutor(
            worker_router=mock_router,
            execution_log_dir=tmp_path,
        )

        duty = _make_duty()
        trigger = duty.triggers[0]

        record = await executor.execute(duty, trigger, "t1", "w1")

        assert "error" in record.conclusion

    @pytest.mark.asyncio
    async def test_execute_treats_error_event_as_failure(self, tmp_path):
        async def fail_stream(*args, **kwargs):
            yield type(
                "Event",
                (),
                {
                    "content": "",
                    "run_id": "r1",
                    "event_type": "ERROR",
                    "message": "router failed",
                },
            )()

        executor = DutyExecutor(
            worker_router=type("Router", (), {"route_stream": fail_stream})(),
            execution_log_dir=tmp_path,
        )

        record = await executor.execute(_make_duty(), _make_duty().triggers[0], "t1", "w1")

        assert record.conclusion == "error: router failed"

    @pytest.mark.asyncio
    async def test_execute_treats_unsuccessful_run_finished_as_failure(self, tmp_path):
        async def fail_stream(*args, **kwargs):
            yield type(
                "Event",
                (),
                {
                    "content": "",
                    "run_id": "r1",
                    "event_type": "RUN_FINISHED",
                    "success": False,
                    "stop_reason": "tool blocked",
                },
            )()

        executor = DutyExecutor(
            worker_router=type("Router", (), {"route_stream": fail_stream})(),
            execution_log_dir=tmp_path,
        )

        record = await executor.execute(_make_duty(), _make_duty().triggers[0], "t1", "w1")

        assert record.conclusion == "error: tool blocked"

    @pytest.mark.asyncio
    async def test_execute_copies_pre_script_into_manifest(self, tmp_path):
        captured = {}

        async def stream(*args, **kwargs):
            captured["manifest"] = kwargs.get("manifest")
            yield type("Event", (), {"content": "done", "run_id": "r1"})()

        executor = DutyExecutor(
            worker_router=type("Router", (), {"route_stream": stream})(),
            execution_log_dir=tmp_path,
        )
        duty = _make_duty()
        duty = Duty(
            duty_id=duty.duty_id,
            title=duty.title,
            status=duty.status,
            triggers=duty.triggers,
            execution_policy=duty.execution_policy,
            action=duty.action,
            quality_criteria=duty.quality_criteria,
            pre_script=InlineScript(source="print('prep')"),
            escalation=duty.escalation,
        )

        await executor.execute(duty, duty.triggers[0], "t1", "w1")

        assert captured["manifest"].pre_script == InlineScript(source="print('prep')")

    @pytest.mark.asyncio
    async def test_execute_with_escalation(self, tmp_path):
        mock_router = AsyncMock()

        async def fake_stream(*args, **kwargs):
            yield type("Event", (), {"content": "anomaly_detected in data", "run_id": "r1"})()

        mock_router.route_stream = fake_stream

        executor = DutyExecutor(
            worker_router=mock_router,
            execution_log_dir=tmp_path,
        )

        duty = _make_duty(
            escalation=EscalationPolicy(
                condition="anomaly_detected",
                target="admin-team",
            ),
        )
        trigger = duty.triggers[0]

        record = await executor.execute(duty, trigger, "t1", "w1")
        assert record.escalated is True

    @pytest.mark.asyncio
    async def test_execute_with_depth_override(self, tmp_path):
        mock_router = AsyncMock()

        async def fake_stream(*args, **kwargs):
            yield type("Event", (), {"content": "ok", "run_id": "r1"})()

        mock_router.route_stream = fake_stream

        executor = DutyExecutor(
            worker_router=mock_router,
            execution_log_dir=tmp_path,
        )

        duty = _make_duty(overrides=(("t1", "deep"),))
        trigger = duty.triggers[0]

        record = await executor.execute(duty, trigger, "t1", "w1")
        assert record.depth == "deep"

    @pytest.mark.asyncio
    async def test_execute_passes_bound_skill_id_to_router(self, tmp_path):
        route_calls = []

        async def fake_stream(*args, **kwargs):
            route_calls.append(kwargs)
            yield type("Event", (), {"content": "ok", "run_id": "r1"})()

        executor = DutyExecutor(
            worker_router=type("Router", (), {"route_stream": fake_stream})(),
            execution_log_dir=tmp_path,
        )

        duty = _make_duty()
        duty = Duty(
            duty_id=duty.duty_id,
            title=duty.title,
            status=duty.status,
            triggers=duty.triggers,
            execution_policy=duty.execution_policy,
            action=duty.action,
            quality_criteria=duty.quality_criteria,
            skill_id="approval-review",
        )

        await executor.execute(duty, duty.triggers[0], "t1", "w1")

        assert route_calls[-1]["skill_id"] == "approval-review"

    @pytest.mark.asyncio
    async def test_execute_without_bound_skill_keeps_legacy_routing(self, tmp_path):
        route_calls = []

        async def fake_stream(*args, **kwargs):
            route_calls.append(kwargs)
            yield type("Event", (), {"content": "ok", "run_id": "r1"})()

        executor = DutyExecutor(
            worker_router=type("Router", (), {"route_stream": fake_stream})(),
            execution_log_dir=tmp_path,
        )

        await executor.execute(_make_duty(), _make_duty().triggers[0], "t1", "w1")

        assert route_calls[-1]["skill_id"] is None
        assert route_calls[-1]["preferred_skill_ids"] == ()

    @pytest.mark.asyncio
    async def test_execute_passes_soft_preferred_skills_to_router(self, tmp_path):
        route_calls = []

        async def fake_stream(*args, **kwargs):
            route_calls.append(kwargs)
            yield type("Event", (), {"content": "ok", "run_id": "r1"})()

        executor = DutyExecutor(
            worker_router=type("Router", (), {"route_stream": fake_stream})(),
            execution_log_dir=tmp_path,
        )

        duty = Duty(
            duty_id="test-duty",
            title="Data Quality Check",
            status="active",
            triggers=(DutyTrigger(id="t1", type="manual"),),
            execution_policy=ExecutionPolicy(),
            action="Check all data sources for quality issues.",
            quality_criteria=("All fields must have values",),
            preferred_skill_ids=("approval-review", "document-analysis"),
        )

        await executor.execute(duty, duty.triggers[0], "t1", "w1")

        assert route_calls[-1]["skill_id"] is None
        assert route_calls[-1]["preferred_skill_ids"] == (
            "approval-review",
            "document-analysis",
        )

    @pytest.mark.asyncio
    async def test_execute_calls_learning_handler(self, tmp_path):
        called = []

        async def fake_stream(*args, **kwargs):
            yield type("Event", (), {"content": "ok", "run_id": "r1"})()

        async def learning_handler(record, duty):
            called.append((record.duty_id, duty.duty_id))

        executor = DutyExecutor(
            worker_router=type("Router", (), {"route_stream": fake_stream})(),
            execution_log_dir=tmp_path,
            duty_learning_handler=learning_handler,
        )

        duty = _make_duty()
        await executor.execute(duty, duty.triggers[0], "t1", "w1")
        assert called == [("test-duty", "test-duty")]
