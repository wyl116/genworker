"""
GOAL.md parser - parses YAML frontmatter + Markdown body into Goal.

Validates:
- goal_id is non-empty
- status is in allowed values
- priority is in allowed values
- blocked_by references point to existing task IDs
"""
from __future__ import annotations

import re
from typing import Any

import yaml

from src.worker.scripts.models import deserialize_pre_script

from .models import (
    ALLOWED_GOAL_STATUSES,
    ALLOWED_MILESTONE_STATUSES,
    ALLOWED_PRIORITIES,
    ALLOWED_TASK_STATUSES,
    ExternalSource,
    Goal,
    GoalTask,
    Milestone,
)

_FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$",
    re.DOTALL,
)


def parse_goal(content: str) -> Goal:
    """
    Parse a GOAL.md string into a Goal.

    Validates goal_id non-empty, status/priority in allowed values,
    and all blocked_by references point to existing task IDs.

    Raises ValueError on validation failures.
    """
    match = _FRONTMATTER_PATTERN.match(content.strip())
    if not match:
        raise ValueError("Invalid GOAL.md: missing YAML frontmatter")

    yaml_section = match.group(1)
    meta = yaml.safe_load(yaml_section)
    if not isinstance(meta, dict):
        raise ValueError("Invalid GOAL.md: frontmatter is not a mapping")

    goal_id = meta.get("goal_id", "")
    if not goal_id:
        raise ValueError("goal_id must not be empty")

    status = meta.get("status", "active")
    if status not in ALLOWED_GOAL_STATUSES:
        raise ValueError(
            f"Invalid goal status '{status}', must be one of {sorted(ALLOWED_GOAL_STATUSES)}"
        )

    priority = meta.get("priority", "medium")
    if priority not in ALLOWED_PRIORITIES:
        raise ValueError(
            f"Invalid priority '{priority}', must be one of {sorted(ALLOWED_PRIORITIES)}"
        )

    milestones = _parse_milestones(meta.get("milestones", []))

    # Validate blocked_by references
    all_task_ids = _collect_task_ids(milestones)
    _validate_blocked_by(milestones, all_task_ids)

    external_source = _parse_external_source(meta.get("external_source"))
    preferred_skill_ids = _parse_preferred_skill_ids(meta)
    default_pre_script = _parse_default_pre_script(meta.get("default_pre_script"))

    return Goal(
        goal_id=goal_id,
        title=meta.get("title", ""),
        status=status,
        priority=priority,
        deadline=meta.get("deadline"),
        created_by=meta.get("created_by", ""),
        approved_by=meta.get("approved_by", ""),
        milestones=milestones,
        preferred_skill_ids=preferred_skill_ids,
        default_pre_script=default_pre_script,
        on_complete=meta.get("on_complete"),
        external_source=external_source,
    )


def _parse_milestones(raw: list[dict[str, Any]] | None) -> tuple[Milestone, ...]:
    """Parse milestone dicts into Milestone objects."""
    if not raw:
        return ()
    results: list[Milestone] = []
    for m in raw:
        ms_status = m.get("status", "pending")
        if ms_status not in ALLOWED_MILESTONE_STATUSES:
            raise ValueError(
                f"Invalid milestone status '{ms_status}', "
                f"must be one of {sorted(ALLOWED_MILESTONE_STATUSES)}"
            )
        tasks = _parse_tasks(m.get("tasks", []))
        results.append(Milestone(
            id=m.get("id", ""),
            title=m.get("title", ""),
            status=ms_status,
            deadline=m.get("deadline"),
            completed_at=m.get("completed_at"),
            tasks=tasks,
        ))
    return tuple(results)


def _parse_tasks(raw: list[dict[str, Any]] | None) -> tuple[GoalTask, ...]:
    """Parse task dicts into GoalTask objects."""
    if not raw:
        return ()
    results: list[GoalTask] = []
    for t in raw:
        t_status = t.get("status", "pending")
        if t_status not in ALLOWED_TASK_STATUSES:
            raise ValueError(
                f"Invalid task status '{t_status}', "
                f"must be one of {sorted(ALLOWED_TASK_STATUSES)}"
            )
        blocked_by = tuple(t.get("blocked_by", ()))
        results.append(GoalTask(
            id=t.get("id", ""),
            title=t.get("title", ""),
            status=t_status,
            notes=t.get("notes", ""),
            blocked_by=blocked_by,
        ))
    return tuple(results)


def _parse_external_source(raw: dict[str, Any] | None) -> ExternalSource | None:
    """Parse optional external source dict."""
    if not raw:
        return None
    return ExternalSource(
        type=raw.get("type", "manual"),
        source_uri=raw.get("source_uri", ""),
        last_synced_at=raw.get("last_synced_at"),
        sync_direction=raw.get("sync_direction", "bidirectional"),
        stakeholders=tuple(raw.get("stakeholders", ())),
        sync_schedule=raw.get("sync_schedule"),
    )


def _parse_preferred_skill_ids(meta: dict[str, Any]) -> tuple[str, ...]:
    """Parse soft-preferred skill ids from GOAL.md frontmatter."""
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


def _parse_default_pre_script(raw: Any) -> object | None:
    """Parse an optional goal-level default pre-script."""
    if raw in (None, "", {}):
        return None
    return deserialize_pre_script(raw)


def _collect_task_ids(milestones: tuple[Milestone, ...]) -> frozenset[str]:
    """Collect all task IDs across all milestones."""
    ids: list[str] = []
    for m in milestones:
        for t in m.tasks:
            ids.append(t.id)
    return frozenset(ids)


def _validate_blocked_by(
    milestones: tuple[Milestone, ...],
    all_task_ids: frozenset[str],
) -> None:
    """Validate that all blocked_by references point to existing task IDs."""
    for m in milestones:
        for t in m.tasks:
            for ref in t.blocked_by:
                if ref not in all_task_ids:
                    raise ValueError(
                        f"Task '{t.id}' has blocked_by reference '{ref}' "
                        f"that does not exist in any milestone"
                    )
