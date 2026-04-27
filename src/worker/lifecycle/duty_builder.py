"""Utilities for materializing lifecycle suggestions into DUTY.md files."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from src.worker.duty.models import Duty, DutyTrigger, ExecutionPolicy
from src.worker.duty.parser import parse_duty

from .file_io import atomic_write_text


def stable_duty_id(seed: str, *, prefix: str = "duty") -> str:
    """Build a stable ASCII-only duty identifier from arbitrary input text."""
    raw = str(seed or "").strip().lower()
    slug = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in raw)
    slug = "-".join(part for part in slug.split("-") if part)[:32]
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    if slug:
        return f"{prefix}-{slug}-{digest}"
    return f"{prefix}-{digest}"


def build_duty_from_payload(payload: dict, *, default_title: str = "") -> Duty:
    """Build a Duty model from a suggestion payload."""
    title = str(payload.get("title", "") or default_title or "Lifecycle duty").strip()
    duty_id = str(payload.get("duty_id", "") or f"duty-{uuid4().hex[:8]}")
    schedule = str(payload.get("schedule", "") or "").strip()
    triggers = (
        DutyTrigger(
            id="schedule-1",
            type="schedule",
            description="Lifecycle generated schedule",
            cron=schedule or "0 9 * * 1",
        ),
    )
    preferred_skill_ids = tuple(
        str(item).strip()
        for item in payload.get("preferred_skill_ids", ())
        if str(item).strip()
    )
    return Duty(
        duty_id=duty_id,
        title=title,
        status="active",
        triggers=triggers,
        execution_policy=ExecutionPolicy(),
        action=str(payload.get("action", "") or title),
        quality_criteria=tuple(
            str(item).strip()
            for item in payload.get("quality_criteria", ())
            if str(item).strip()
        ) or ("产出结果符合原目标要求",),
        skill_hint=None,
        skill_id=None,
        preferred_skill_ids=preferred_skill_ids,
    )


def write_duty_md(duty: Duty, duties_dir: Path, *, filename: str | None = None) -> Path:
    """Serialize a Duty into DUTY.md frontmatter + body."""
    duties_dir.mkdir(parents=True, exist_ok=True)
    if filename is not None:
        path = duties_dir / filename
    else:
        existing = find_duty_file(duties_dir, duty.duty_id)
        if existing is not None:
            path = existing
        else:
            path = duties_dir / f"{_sanitize_filename(duty.duty_id or duty.title)}.md"
    atomic_write_text(path, duty_to_markdown(duty))
    return path


def duty_to_markdown(duty: Duty) -> str:
    """Serialize a Duty model into markdown."""
    lines = [
        "---",
        f"duty_id: {duty.duty_id}",
        f"title: {json.dumps(duty.title, ensure_ascii=False)}",
        f"status: {duty.status}",
        "triggers:",
    ]
    for trigger in duty.triggers:
        lines.append(f"  - id: {trigger.id}")
        lines.append(f"    type: {trigger.type}")
        if trigger.description:
            lines.append(f"    description: {json.dumps(trigger.description, ensure_ascii=False)}")
        if trigger.cron:
            lines.append(f"    cron: {json.dumps(trigger.cron)}")
        if trigger.source:
            lines.append(f"    source: {json.dumps(trigger.source)}")
    lines.extend([
        "execution_policy:",
        f"  default: {duty.execution_policy.default}",
    ])
    if duty.execution_policy.overrides:
        lines.append("  overrides:")
        for trigger_id, depth in duty.execution_policy.overrides:
            lines.append(f"    {trigger_id}: {depth}")
    if duty.escalation is not None:
        lines.append("escalation:")
        lines.append(f"  condition: {json.dumps(duty.escalation.condition, ensure_ascii=False)}")
        lines.append(f"  target: {json.dumps(duty.escalation.target, ensure_ascii=False)}")
    if duty.execution_log_retention:
        lines.append(f"execution_log_retention: {duty.execution_log_retention}")
    lines.append("quality_criteria:")
    for criterion in duty.quality_criteria:
        lines.append(f"  - {json.dumps(criterion, ensure_ascii=False)}")
    if duty.skill_id:
        lines.append(f"skill_id: {duty.skill_id}")
    elif duty.skill_hint:
        lines.append(f"skill_hint: {duty.skill_hint}")
    if duty.preferred_skill_ids:
        lines.append("preferred_skill_ids:")
        for skill_id in duty.preferred_skill_ids:
            lines.append(f"  - {skill_id}")
    lines.append("---")
    lines.append(duty.action.strip())
    lines.append("")
    return "\n".join(lines)


def apply_duty_redefine(duties_dir: Path, duty_id: str, payload: dict) -> Duty | None:
    """Apply a redefine/pause suggestion to an existing duty file."""
    target = find_duty_file(duties_dir, duty_id)
    if target is None:
        return None
    duty = parse_duty(target.read_text(encoding="utf-8"))
    recommended_action = str(payload.get("recommended_action", "") or "")
    suggested_changes = payload.get("suggested_changes", {})
    if not isinstance(suggested_changes, dict):
        suggested_changes = {}
    if recommended_action == "pause":
        duty = replace(duty, status="closed")
    if "action" in suggested_changes:
        duty = replace(duty, action=str(suggested_changes["action"]).strip() or duty.action)
    if "quality_criteria" in suggested_changes:
        criteria = tuple(
            str(item).strip()
            for item in suggested_changes.get("quality_criteria", ())
            if str(item).strip()
        )
        if criteria:
            duty = replace(duty, quality_criteria=criteria)
    atomic_write_text(target, duty_to_markdown(duty))
    return duty


def find_duty_file(duties_dir: Path, duty_id: str) -> Path | None:
    """Locate a duty markdown file by parsed duty_id."""
    if not duties_dir.is_dir():
        return None
    for duty_file in sorted(duties_dir.glob("*.md")):
        try:
            duty = parse_duty(duty_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if duty.duty_id == duty_id:
            return duty_file
    return None


def _sanitize_filename(title: str) -> str:
    safe = title.lower().replace(" ", "-")
    return "".join(ch for ch in safe if ch.isalnum() or ch in "-_.")[:64] or "duty"
