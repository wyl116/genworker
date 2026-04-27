"""
DUTY.md parser - extracts duty definitions from YAML frontmatter.

Validates:
- duty_id is non-empty
- At least one trigger
- Trigger types in allowed set
- schedule triggers must have cron
- event triggers must have source
- condition triggers must have metric and rule
- At least one quality criterion
"""
import frontmatter

from src.worker.scripts.models import deserialize_pre_script

from .models import (
    ALLOWED_DEPTHS,
    ALLOWED_TRIGGER_TYPES,
    Duty,
    DutyTrigger,
    EscalationPolicy,
    ExecutionPolicy,
)


class DutyParseError(ValueError):
    """Raised when a DUTY.md file has invalid format or content."""
    pass


def parse_duty(content: str) -> Duty:
    """
    Parse a DUTY.md file content into a Duty dataclass.

    Expects YAML frontmatter with duty definition fields
    and a Markdown body for the action description.
    """
    try:
        post = frontmatter.loads(content)
    except Exception as exc:
        raise DutyParseError(f"Failed to parse YAML frontmatter: {exc}") from exc

    meta = post.metadata
    body = post.content.strip()

    duty_id = _require_str(meta, "duty_id")
    title = _require_str(meta, "title")
    status = meta.get("status", "active")

    triggers = _parse_triggers(meta.get("triggers", []))
    execution_policy = _parse_execution_policy(meta.get("execution_policy", {}))
    quality_criteria = _parse_quality_criteria(meta.get("quality_criteria", []))
    skill_id = _optional_str(meta.get("skill_id"))
    skill_hint = _optional_str(meta.get("skill_hint"))
    preferred_skill_ids = _parse_preferred_skill_ids(meta)
    pre_script = _parse_pre_script(meta.get("pre_script"))
    escalation = _parse_escalation(meta.get("escalation"))
    retention = meta.get("execution_log_retention", "30d")

    action = body if body else meta.get("action", "")
    if not action:
        raise DutyParseError("Duty must have an action (in body or frontmatter)")

    return Duty(
        duty_id=duty_id,
        title=title,
        status=status,
        triggers=triggers,
        execution_policy=execution_policy,
        action=action,
        quality_criteria=quality_criteria,
        skill_hint=skill_hint or skill_id,
        skill_id=skill_id or skill_hint,
        preferred_skill_ids=preferred_skill_ids,
        pre_script=pre_script,
        escalation=escalation,
        execution_log_retention=retention,
    )


def _require_str(meta: dict, key: str) -> str:
    """Extract a required non-empty string field."""
    value = meta.get(key)
    if not value or not str(value).strip():
        raise DutyParseError(f"'{key}' is required and must be non-empty")
    return str(value).strip()


def _optional_str(value: object) -> str | None:
    """Normalize optional string metadata fields."""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _parse_preferred_skill_ids(meta: dict) -> tuple[str, ...]:
    """Parse non-binding preferred skills from frontmatter."""
    raw = meta.get("preferred_skill_ids")
    if raw is None:
        raw = meta.get("skills", ())
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    return tuple(
        str(skill_id).strip()
        for skill_id in raw
        if str(skill_id).strip()
    )


def _parse_triggers(raw_triggers: list) -> tuple[DutyTrigger, ...]:
    """Parse and validate trigger list."""
    if not raw_triggers:
        raise DutyParseError("At least one trigger is required")

    triggers = []
    for raw in raw_triggers:
        if not isinstance(raw, dict):
            raise DutyParseError(f"Trigger must be a dict, got {type(raw).__name__}")

        trigger_id = raw.get("id", "")
        if not trigger_id:
            raise DutyParseError("Trigger 'id' is required")

        trigger_type = raw.get("type", "")
        if trigger_type not in ALLOWED_TRIGGER_TYPES:
            raise DutyParseError(
                f"Trigger type '{trigger_type}' not allowed. "
                f"Must be one of: {sorted(ALLOWED_TRIGGER_TYPES)}"
            )

        _validate_trigger_fields(raw, trigger_type)

        filter_raw = raw.get("filter", {})
        filter_tuples = tuple(
            (str(k), str(v)) for k, v in filter_raw.items()
        ) if isinstance(filter_raw, dict) else ()

        check_interval = raw.get("check_interval", "5m")

        triggers.append(DutyTrigger(
            id=trigger_id,
            type=trigger_type,
            description=raw.get("description", ""),
            cron=raw.get("cron"),
            source=raw.get("source"),
            filter=filter_tuples,
            metric=raw.get("metric"),
            rule=raw.get("rule"),
            check_interval=check_interval,
        ))

    return tuple(triggers)


def _validate_trigger_fields(raw: dict, trigger_type: str) -> None:
    """Validate required fields based on trigger type."""
    if trigger_type == "schedule":
        if not raw.get("cron"):
            raise DutyParseError("schedule trigger must have 'cron'")
    elif trigger_type == "event":
        if not raw.get("source"):
            raise DutyParseError("event trigger must have 'source'")
    elif trigger_type == "condition":
        if not raw.get("metric"):
            raise DutyParseError("condition trigger must have 'metric'")
        if not raw.get("rule"):
            raise DutyParseError("condition trigger must have 'rule'")


def _parse_execution_policy(raw: dict) -> ExecutionPolicy:
    """Parse execution policy with depth validation."""
    if not raw:
        return ExecutionPolicy()

    default = raw.get("default", "standard")
    if default not in ALLOWED_DEPTHS:
        raise DutyParseError(
            f"Execution depth '{default}' not allowed. "
            f"Must be one of: {sorted(ALLOWED_DEPTHS)}"
        )

    overrides_raw = raw.get("overrides", {})
    overrides = tuple(
        (str(k), str(v)) for k, v in overrides_raw.items()
    ) if isinstance(overrides_raw, dict) else ()

    for _, depth in overrides:
        if depth not in ALLOWED_DEPTHS:
            raise DutyParseError(
                f"Override depth '{depth}' not allowed. "
                f"Must be one of: {sorted(ALLOWED_DEPTHS)}"
            )

    return ExecutionPolicy(default=default, overrides=overrides)


def _parse_quality_criteria(raw: list) -> tuple[str, ...]:
    """Parse and validate quality criteria list."""
    if not raw:
        raise DutyParseError("At least one quality criterion is required")
    return tuple(str(c).strip() for c in raw if str(c).strip())


def _parse_escalation(raw: dict | None) -> EscalationPolicy | None:
    """Parse optional escalation policy."""
    if not raw:
        return None
    condition = raw.get("condition", "")
    target = raw.get("target", "")
    if not condition or not target:
        raise DutyParseError(
            "Escalation policy requires both 'condition' and 'target'"
        )
    return EscalationPolicy(condition=condition, target=target)


def _parse_pre_script(raw) -> object | None:
    """Parse an optional pre_script block."""
    if raw in (None, "", {}):
        return None
    try:
        return deserialize_pre_script(raw)
    except Exception as exc:
        raise DutyParseError(f"Invalid pre_script: {exc}") from exc
