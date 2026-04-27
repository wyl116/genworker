"""
GoalGenerator - convert ParsedGoalInfo into Goal objects.

Provides:
- generate_goal_from_parsed: create a new Goal from parsed external content
- update_goal_from_external: update an existing Goal from new external content
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from src.services.llm.intent import LLMCallIntent, Purpose
from src.worker.goal.models import (
    ALLOWED_GOAL_STATUSES,
    ALLOWED_MILESTONE_STATUSES,
    ALLOWED_PRIORITIES,
    ALLOWED_TASK_STATUSES,
)
from src.worker.goal.models import (
    ExternalSource,
    Goal,
    GoalTask,
    Milestone,
)
from src.worker.goal.parser import parse_goal
from src.worker.scripts.models import InlineScript, ScriptRef

from .domain_models import ParsedGoalInfo

logger = logging.getLogger(__name__)


class LLMClient(Protocol):
    """Protocol for LLM invocation."""
    async def invoke(
        self, messages: list[dict[str, str]], **kwargs: Any,
    ) -> Any: ...


class EventPublisher(Protocol):
    """Protocol for publishing events."""
    async def publish(self, event: Any) -> int: ...


async def generate_goal_from_parsed(
    parsed: ParsedGoalInfo,
    goals_dir: Path,
    require_approval: bool = True,
    event_bus: EventPublisher | None = None,
    tenant_id: str = "demo",
    worker_id: str = "",
) -> Goal:
    """
    Create a Goal from ParsedGoalInfo.

    Steps:
    1. Build Goal object with ExternalSource linking to original source
    2. Convert milestone dicts to Milestone/GoalTask objects
    3. Set status to "pending_approval" if require_approval else "active"
    4. Write GOAL.md to goals_dir
    5. Publish "goal.created_from_external" event
    """
    goal_id = f"goal-ext-{uuid.uuid4().hex[:8]}"
    status = "pending_approval" if require_approval else "active"
    if status not in ALLOWED_GOAL_STATUSES:
        raise ValueError(
            f"Generated goal status '{status}' not in ALLOWED_GOAL_STATUSES "
            f"({sorted(ALLOWED_GOAL_STATUSES)}). This is a coding error."
        )

    external_source = ExternalSource(
        type=parsed.source_type,
        source_uri=parsed.source_uri,
        sync_direction="bidirectional",
        stakeholders=parsed.stakeholders,
    )

    milestones = _build_milestones(parsed.milestones)

    goal = Goal(
        goal_id=goal_id,
        title=parsed.title,
        status=status,
        priority=parsed.priority,
        deadline=parsed.deadline,
        milestones=milestones,
        preferred_skill_ids=(),
        external_source=external_source,
    )

    write_goal_md(goal, goals_dir)

    if event_bus is not None:
        await _publish_goal_created(event_bus, goal, parsed, tenant_id, worker_id)

    logger.info(
        f"[GoalGenerator] Created goal '{goal_id}' from "
        f"{parsed.source_type} (status={status})"
    )
    return goal


async def update_goal_from_external(
    goal: Goal,
    new_content: str,
    llm_client: LLMClient,
) -> Goal:
    """
    Update a Goal when its external document has changed.

    Uses LLM to compare current goal state with new content,
    identifies new/modified milestones. On conflict, marks
    goal for manual review.
    """
    prompt = _build_diff_prompt(goal, new_content)
    messages = [{"role": "user", "content": prompt}]

    try:
        response = await llm_client.invoke(
            messages=messages,
            intent=LLMCallIntent(purpose=Purpose.EXTRACT),
        )
        content = (
            response if isinstance(response, str)
            else getattr(response, "content", str(response))
        )
        return _apply_update(goal, content)
    except Exception as exc:
        logger.error(f"[GoalGenerator] Update from external failed: {exc}")
        return goal


def _build_milestones(
    milestone_dicts: tuple[dict[str, Any], ...],
) -> tuple[Milestone, ...]:
    """Convert milestone dicts to Milestone objects."""
    result: list[Milestone] = []
    for i, md in enumerate(milestone_dicts):
        ms_id = md.get("id", f"ms-ext-{i+1}")
        tasks_raw = md.get("tasks", [])
        tasks = tuple(
            GoalTask(
                id=t.get("id", f"{ms_id}-t{j+1}"),
                title=str(t.get("title", "")),
                status="pending",
            )
            for j, t in enumerate(tasks_raw)
        )
        result.append(Milestone(
            id=ms_id,
            title=str(md.get("title", f"Milestone {i+1}")),
            status="pending",
            deadline=md.get("deadline"),
            tasks=tasks,
        ))
    return tuple(result)


def goal_to_markdown(goal: Goal) -> str:
    """Serialize a Goal to GOAL.md markdown."""
    lines = [
        "---",
        f"goal_id: {goal.goal_id}",
        f"title: {json.dumps(goal.title, ensure_ascii=False)}",
        f"status: {goal.status}",
        f"priority: {goal.priority}",
    ]
    if goal.created_by:
        lines.append(f"created_by: {json.dumps(goal.created_by, ensure_ascii=False)}")
    if goal.approved_by:
        lines.append(f"approved_by: {json.dumps(goal.approved_by, ensure_ascii=False)}")
    if goal.deadline:
        lines.append(f'deadline: "{goal.deadline}"')
    if goal.on_complete:
        lines.append(f"on_complete: {json.dumps(goal.on_complete, ensure_ascii=False)}")
    if goal.external_source:
        lines.append("external_source:")
        lines.append(f"  type: {goal.external_source.type}")
        lines.append(f"  source_uri: {goal.external_source.source_uri}")
        lines.append(
            f"  sync_direction: {goal.external_source.sync_direction}"
        )
        if goal.external_source.last_synced_at:
            lines.append(
                f"  last_synced_at: {goal.external_source.last_synced_at}"
            )
        if goal.external_source.sync_schedule:
            lines.append(
                f"  sync_schedule: {goal.external_source.sync_schedule}"
            )
        if goal.external_source.stakeholders:
            lines.append("  stakeholders:")
            for sh in goal.external_source.stakeholders:
                lines.append(f"    - {sh}")
    if goal.milestones:
        lines.append("milestones:")
        for ms in goal.milestones:
            lines.append(f"  - id: {ms.id}")
            lines.append(f"    title: {ms.title}")
            lines.append(f"    status: {ms.status}")
            if ms.deadline:
                lines.append(f'    deadline: "{ms.deadline}"')
            if ms.completed_at:
                lines.append(f'    completed_at: "{ms.completed_at}"')
            if ms.tasks:
                lines.append("    tasks:")
                for t in ms.tasks:
                    lines.append(f"      - id: {t.id}")
                    lines.append(f"        title: {t.title}")
                    lines.append(f"        status: {t.status}")
                    if t.notes:
                        lines.append(f"        notes: {t.notes}")
                    if t.blocked_by:
                        lines.append("        blocked_by:")
                        for dep in t.blocked_by:
                            lines.append(f"          - {dep}")
    if goal.preferred_skill_ids:
        lines.append("preferred_skill_ids:")
        for skill_id in goal.preferred_skill_ids:
            lines.append(f"  - {skill_id}")
    if goal.default_pre_script is not None:
        lines.extend(_pre_script_lines(goal.default_pre_script))
    lines.append("---")
    lines.append(f"# {goal.title}")
    lines.append("")
    return "\n".join(lines)


def write_goal_md(
    goal: Goal,
    goals_dir: Path,
    filename: str | None = None,
) -> Path:
    """Serialize a Goal to GOAL.md format and write to disk."""
    goals_dir.mkdir(parents=True, exist_ok=True)
    if filename is not None:
        filepath = goals_dir / filename
    else:
        existing = find_goal_file(goals_dir, goal.goal_id)
        if existing is not None:
            filepath = existing
        else:
            filepath = goals_dir / f"{_sanitize_filename(goal.goal_id or goal.title)}.md"
    filepath.write_text(goal_to_markdown(goal), encoding="utf-8")
    logger.debug(f"[GoalGenerator] Wrote {filepath}")
    return filepath


def _pre_script_lines(pre_script) -> list[str]:
    lines = ["default_pre_script:"]
    if isinstance(pre_script, InlineScript):
        lines.append("  kind: inline")
        lines.append("  source: |")
        source_lines = pre_script.source.splitlines() or [""]
        for line in source_lines:
            lines.append(f"    {line}")
        if pre_script.enabled_tools:
            lines.append("  enabled_tools:")
            for tool_name in pre_script.enabled_tools:
                lines.append(f"    - {tool_name}")
        lines.append(f"  timeout_seconds: {pre_script.timeout_seconds}")
        lines.append(f"  max_tool_calls: {pre_script.max_tool_calls}")
        return lines
    if isinstance(pre_script, ScriptRef):
        lines.append("  kind: ref")
        lines.append(f"  tool_name: {pre_script.tool_name}")
        if pre_script.tool_input:
            lines.append("  tool_input:")
            for key, value in pre_script.tool_input:
                lines.append(f"    {key}: {json.dumps(value, ensure_ascii=False)}")
        return lines
    raise TypeError(f"Unsupported default_pre_script type: {type(pre_script)!r}")


def find_goal_file(goals_dir: Path, goal_id: str) -> Path | None:
    """Locate a goal markdown file by parsed goal_id."""
    if not goals_dir.is_dir():
        return None
    for goal_file in sorted(goals_dir.glob("*.md")):
        try:
            goal = parse_goal(goal_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if goal.goal_id == goal_id:
            return goal_file
    return None


def find_goal_file_by_source_uri(goals_dir: Path, source_uri: str) -> Path | None:
    """Locate a goal markdown file by external source URI."""
    wanted = str(source_uri or "").strip()
    if not wanted or not goals_dir.is_dir():
        return None
    for goal_file in sorted(goals_dir.glob("*.md")):
        try:
            goal = parse_goal(goal_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if goal.external_source is not None and goal.external_source.source_uri == wanted:
            return goal_file
    return None


def _sanitize_filename(title: str) -> str:
    """Convert a title to a safe filename."""
    safe = title.lower().replace(" ", "-")
    return "".join(c for c in safe if c.isalnum() or c in "-_.")[:64]


async def _publish_goal_created(
    event_bus: EventPublisher,
    goal: Goal,
    parsed: ParsedGoalInfo,
    tenant_id: str,
    worker_id: str,
) -> None:
    """Publish goal.created_from_external event."""
    from src.events.models import Event

    event = Event(
        event_id=f"evt-{uuid.uuid4().hex[:8]}",
        type="goal.created_from_external",
        source="goal_generator",
        tenant_id=tenant_id,
        payload=(
            ("goal_id", goal.goal_id),
            ("source_type", parsed.source_type),
            ("source_uri", parsed.source_uri),
            ("title", goal.title),
            ("require_approval", goal.status == "pending_approval"),
            ("confidence", parsed.confidence),
            ("worker_id", worker_id),
        ),
    )
    await event_bus.publish(event)


def _build_diff_prompt(goal: Goal, new_content: str) -> str:
    """Build a prompt for LLM to compare goal with new external content."""
    current_milestones = ", ".join(
        f"{ms.title}({ms.status})" for ms in goal.milestones
    )
    return (
        f"Current goal: {goal.title}\n"
        f"Current milestones: {current_milestones}\n"
        f"Current status: {goal.status}\n\n"
        f"New external content:\n---\n{new_content}\n---\n\n"
        "Compare the current goal with the new content and respond with JSON. "
        "If there is a true contradiction, respond with "
        '{"conflict": true, "details": "..."}.\n'
        "Otherwise respond with JSON using this schema: "
        '{"conflict": false, "summary": "...", '
        '"goal": {"title": "...", "status": "...", "priority": "...", "deadline": "..."}, '
        '"milestones": ['
        '{"id": "...", "title": "...", "status": "...", "deadline": "...", '
        '"completed_at": "...", "action": "upsert", '
        '"tasks": [{"id": "...", "title": "...", "status": "...", "notes": "...", '
        '"blocked_by": ["..."], "action": "upsert"}]}'
        "]}. "
        "Only include fields that should change."
    )


def _apply_update(goal: Goal, llm_response: str) -> Goal:
    """Apply LLM-identified updates to a Goal."""
    import json

    try:
        data = json.loads(llm_response.strip())
    except (json.JSONDecodeError, ValueError):
        logger.warning("[GoalGenerator] Could not parse LLM diff response")
        return goal

    if data.get("conflict", False):
        logger.warning(
            f"[GoalGenerator] Conflict detected for goal '{goal.goal_id}': "
            f"{data.get('details', 'unknown')}"
        )
        # Mark external_source with conflict info
        if goal.external_source:
            updated_source = replace(
                goal.external_source,
                last_synced_at="conflict_detected",
            )
            return replace(goal, external_source=updated_source)
        return goal

    goal_updates, milestone_updates = _extract_update_payload(data)
    updated_goal = _apply_goal_updates(goal, goal_updates)
    if milestone_updates is not None:
        updated_goal = replace(
            updated_goal,
            milestones=_merge_milestones(
                updated_goal.milestones,
                milestone_updates,
            ),
        )

    if updated_goal.external_source is not None:
        updated_goal = replace(
            updated_goal,
            external_source=replace(
                updated_goal.external_source,
                last_synced_at=_now_iso(),
            ),
        )

    logger.info(
        f"[GoalGenerator] No conflict for goal '{goal.goal_id}': "
        f"{data.get('summary') or data.get('updates', 'applied updates')}"
    )
    return updated_goal


def _extract_update_payload(
    data: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    """Extract goal-level and milestone-level updates from LLM output."""
    goal_updates: dict[str, Any] = {}
    milestone_updates: list[dict[str, Any]] | None = None

    if isinstance(data.get("goal"), dict):
        goal_updates.update(dict(data["goal"]))

    if isinstance(data.get("updates"), dict):
        updates_dict = dict(data["updates"])
        if isinstance(updates_dict.get("goal"), dict):
            goal_updates.update(dict(updates_dict["goal"]))
        else:
            for field in ("title", "status", "priority", "deadline"):
                if field in updates_dict:
                    goal_updates[field] = updates_dict[field]
        if isinstance(updates_dict.get("milestones"), list):
            milestone_updates = list(updates_dict["milestones"])

    for field in ("title", "status", "priority", "deadline"):
        if field in data:
            goal_updates[field] = data[field]

    if isinstance(data.get("milestones"), list):
        milestone_updates = list(data["milestones"])

    return goal_updates, milestone_updates


def _apply_goal_updates(goal: Goal, updates: dict[str, Any]) -> Goal:
    """Apply validated goal-level fields."""
    title = str(updates.get("title", goal.title)).strip() or goal.title
    status = _normalize_choice(
        updates.get("status"), ALLOWED_GOAL_STATUSES, goal.status,
    )
    priority = _normalize_choice(
        updates.get("priority"), ALLOWED_PRIORITIES, goal.priority,
    )
    deadline = _normalize_optional_text(
        updates.get("deadline"),
        goal.deadline,
    )
    return replace(
        goal,
        title=title,
        status=status,
        priority=priority,
        deadline=deadline,
    )


def _merge_milestones(
    existing: tuple[Milestone, ...],
    updates: list[dict[str, Any]],
) -> tuple[Milestone, ...]:
    """Merge milestone updates by id/title, preserving existing data."""
    result = list(existing)
    for index, raw_update in enumerate(updates, 1):
        if not isinstance(raw_update, dict):
            continue

        match_index = _find_named_index(
            result,
            raw_update.get("id"),
            raw_update.get("title"),
        )
        action = str(raw_update.get("action", "upsert")).strip().lower()
        if action == "remove":
            if match_index is not None:
                result.pop(match_index)
            continue

        current = result[match_index] if match_index is not None else None
        milestone_id = _resolve_entity_id(
            raw_update.get("id"),
            current.id if current is not None else "",
            f"ms-generated-{index}",
        )
        title = _resolve_title(
            raw_update.get("title"),
            current.title if current is not None else milestone_id,
        )
        status = _normalize_choice(
            raw_update.get("status"),
            ALLOWED_MILESTONE_STATUSES,
            current.status if current is not None else "pending",
        )
        deadline = _normalize_optional_text(
            raw_update.get("deadline"),
            current.deadline if current is not None else None,
        )
        completed_at = _normalize_optional_text(
            raw_update.get("completed_at"),
            current.completed_at if current is not None else None,
        )
        tasks = (
            _merge_tasks(
                current.tasks if current is not None else (),
                raw_update["tasks"],
                milestone_id,
            )
            if isinstance(raw_update.get("tasks"), list)
            else current.tasks if current is not None else ()
        )
        merged = Milestone(
            id=milestone_id,
            title=title,
            status=status,
            deadline=deadline,
            completed_at=completed_at,
            tasks=tasks,
        )
        if match_index is None:
            result.append(merged)
        else:
            result[match_index] = merged
    return tuple(result)


def _merge_tasks(
    existing: tuple[GoalTask, ...],
    updates: list[dict[str, Any]],
    milestone_id: str,
) -> tuple[GoalTask, ...]:
    """Merge task updates by id/title, preserving existing order."""
    result = list(existing)
    for index, raw_update in enumerate(updates, 1):
        if not isinstance(raw_update, dict):
            continue

        match_index = _find_named_index(
            result,
            raw_update.get("id"),
            raw_update.get("title"),
        )
        action = str(raw_update.get("action", "upsert")).strip().lower()
        if action == "remove":
            if match_index is not None:
                result.pop(match_index)
            continue

        current = result[match_index] if match_index is not None else None
        task_id = _resolve_entity_id(
            raw_update.get("id"),
            current.id if current is not None else "",
            f"{milestone_id}-task-{index}",
        )
        title = _resolve_title(
            raw_update.get("title"),
            current.title if current is not None else task_id,
        )
        status = _normalize_choice(
            raw_update.get("status"),
            ALLOWED_TASK_STATUSES,
            current.status if current is not None else "pending",
        )
        notes = str(raw_update.get("notes", current.notes if current else "")).strip()
        blocked_by = _normalize_blocked_by(
            raw_update.get("blocked_by"),
            current.blocked_by if current is not None else (),
        )
        merged = GoalTask(
            id=task_id,
            title=title,
            status=status,
            notes=notes,
            blocked_by=blocked_by,
        )
        if match_index is None:
            result.append(merged)
        else:
            result[match_index] = merged
    return tuple(result)


def _find_named_index(
    items: list[Any],
    item_id: Any,
    title: Any,
) -> int | None:
    """Find a milestone/task by id first, then by title."""
    item_id_str = str(item_id).strip()
    if item_id_str:
        for index, item in enumerate(items):
            if getattr(item, "id", "") == item_id_str:
                return index
    title_str = str(title).strip().lower()
    if title_str:
        for index, item in enumerate(items):
            if getattr(item, "title", "").strip().lower() == title_str:
                return index
    return None


def _resolve_entity_id(raw_value: Any, fallback: str, default: str) -> str:
    """Resolve a non-empty entity id from update payload."""
    value = str(raw_value or fallback or default).strip()
    return value or default


def _resolve_title(raw_value: Any, fallback: str) -> str:
    """Resolve a non-empty title from update payload."""
    value = str(raw_value or fallback).strip()
    return value or fallback


def _normalize_choice(
    raw_value: Any,
    allowed_values: frozenset[str],
    fallback: str,
) -> str:
    """Normalize a status/priority value against allowed constants."""
    value = str(raw_value).strip().lower() if raw_value is not None else ""
    return value if value in allowed_values else fallback


def _normalize_optional_text(raw_value: Any, fallback: str | None) -> str | None:
    """Normalize optional free text fields while preserving None."""
    if raw_value is None:
        return fallback
    value = str(raw_value).strip()
    return value or None


def _normalize_blocked_by(
    raw_value: Any,
    fallback: tuple[str, ...],
) -> tuple[str, ...]:
    """Normalize blocked_by into a tuple of strings."""
    if raw_value is None:
        return fallback
    if isinstance(raw_value, (list, tuple)):
        return tuple(str(item).strip() for item in raw_value if str(item).strip())
    value = str(raw_value).strip()
    return (value,) if value else ()


def _now_iso() -> str:
    """Current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()
