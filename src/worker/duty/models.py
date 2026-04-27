"""
Duty data models - all frozen dataclasses for immutability.

Defines:
- DutyTrigger: trigger configuration (schedule/event/condition/collaboration/manual)
- EventContext: immutable event snapshot for reactive duties
- ExecutionPolicy: depth selection with per-trigger overrides
- EscalationPolicy: escalation conditions and targets
- Duty: complete duty definition from DUTY.md
- DutyExecutionRecord: single execution log entry
"""
from dataclasses import dataclass
from typing import Any

from src.worker.scripts.models import PreScript


ALLOWED_TRIGGER_TYPES = frozenset({
    "schedule", "event", "condition", "collaboration", "manual",
})

ALLOWED_DEPTHS = frozenset({"quick", "standard", "deep"})


@dataclass(frozen=True)
class DutyTrigger:
    """Configuration for a single duty trigger."""
    id: str
    type: str              # "schedule" | "event" | "condition" | "collaboration" | "manual"
    description: str = ""
    cron: str | None = None
    source: str | None = None
    filter: tuple[tuple[str, str], ...] = ()
    metric: str | None = None
    rule: str | None = None
    check_interval: str = "5m"


@dataclass(frozen=True)
class EventContext:
    """Immutable snapshot of the event that triggered a duty."""

    event_id: str
    event_type: str
    payload: tuple[tuple[str, Any], ...] = ()
    source: str = ""

    @property
    def payload_dict(self) -> dict[str, Any]:
        return dict(self.payload)

    def summary(self, max_fields: int = 10) -> str:
        """Format a concise event summary for prompt injection."""
        lines = [
            f"Event: {self.event_type} (id={self.event_id}, source={self.source})",
        ]
        for key, value in self.payload[:max_fields]:
            value_text = str(value)
            if len(value_text) > 500:
                value_text = f"{value_text[:500]}..."
            lines.append(f"  {key}: {value_text}")
        if len(self.payload) > max_fields:
            lines.append(f"  ... and {len(self.payload) - max_fields} more fields")
        return "\n".join(lines)


@dataclass(frozen=True)
class ExecutionPolicy:
    """Execution depth policy with per-trigger overrides."""
    default: str = "standard"      # "quick" | "standard" | "deep"
    overrides: tuple[tuple[str, str], ...] = ()  # ((trigger_id, depth), ...)


@dataclass(frozen=True)
class EscalationPolicy:
    """Escalation configuration."""
    condition: str
    target: str


@dataclass(frozen=True)
class Duty:
    """
    Complete duty definition parsed from DUTY.md.

    Immutable - use dataclasses.replace() for modifications.
    """
    duty_id: str
    title: str
    status: str            # "active" | "closed" | "deprecated"
    triggers: tuple[DutyTrigger, ...]
    execution_policy: ExecutionPolicy
    action: str            # Markdown execution description
    quality_criteria: tuple[str, ...]
    skill_hint: str | None = None
    skill_id: str | None = None
    preferred_skill_ids: tuple[str, ...] = ()
    pre_script: PreScript | None = None
    escalation: EscalationPolicy | None = None
    execution_log_retention: str = "30d"

    @property
    def preferred_skill_id(self) -> str | None:
        """Return the explicit skill binding, or fall back to legacy hint."""
        return self.skill_id or self.skill_hint or None

    @property
    def soft_preferred_skill_ids(self) -> tuple[str, ...]:
        """Return non-binding preferred skills in priority order."""
        if self.preferred_skill_ids:
            return self.preferred_skill_ids
        if self.skill_hint:
            return (self.skill_hint,)
        return ()

    def depth_for_trigger(self, trigger_id: str) -> str:
        """
        Determine execution depth for a given trigger.

        Checks overrides first, falls back to default.
        """
        overrides_dict = dict(self.execution_policy.overrides)
        return overrides_dict.get(trigger_id, self.execution_policy.default)


@dataclass(frozen=True)
class DutyExecutionRecord:
    """Single execution log entry for a duty run."""
    execution_id: str
    duty_id: str
    trigger_id: str
    depth: str
    executed_at: str       # ISO 8601
    duration_seconds: float
    conclusion: str
    anomalies_found: tuple[str, ...] = ()
    escalated: bool = False
    task_id: str | None = None
