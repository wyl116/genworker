# edition: baseline
from __future__ import annotations

from src.worker.duty.duty_executor import build_duty_prompt
from src.worker.duty.models import Duty, DutyTrigger, EventContext, ExecutionPolicy


def _make_duty() -> Duty:
    return Duty(
        duty_id="email-triage",
        title="Email Triage",
        status="active",
        triggers=(DutyTrigger(id="evt-1", type="event", source="external.email_received"),),
        execution_policy=ExecutionPolicy(),
        action="Triage the incoming email.",
        quality_criteria=("Classify correctly",),
    )


def test_event_context_summary_truncates_large_values() -> None:
    context = EventContext(
        event_id="evt-1",
        event_type="external.email_received",
        source="sensor:email",
        payload=(("content", "x" * 600), ("subject", "URGENT")),  # noqa: RUF001
    )

    summary = context.summary()

    assert "external.email_received" in summary
    assert "x" * 500 in summary
    assert "x" * 550 not in summary
    assert "..." in summary


def test_build_duty_prompt_includes_triggering_event() -> None:
    prompt = build_duty_prompt(
        _make_duty(),
        "standard",
        DutyTrigger(id="evt-1", type="event", source="external.email_received"),
        EventContext(
            event_id="evt-1",
            event_type="external.email_received",
            source="sensor:email",
            payload=(("subject", "URGENT: DB down"), ("from", "alert@corp.com")),
        ),
    )

    assert "## Triggering Event" in prompt
    assert "URGENT: DB down" in prompt
    assert "alert@corp.com" in prompt
