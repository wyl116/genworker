"""
Task management tools - Internal step tracking for Workers.

Provides task_create, task_get, task_list, task_update tools
for LLM self-management during complex multi-step work.

Each tool set shares a TaskStore instance scoped to one
execution run (not persisted across runs).
"""
import json
from typing import Any

from src.common.logger import get_logger

from .task_store import Task, TaskStatus, TaskStore
from ..mcp.tool import Tool
from ..mcp.types import MCPCategory, RiskLevel, ToolType

logger = get_logger()


def _format_task(task: Task) -> str:
    """Format a single task for LLM output."""
    status_icon = {
        TaskStatus.PENDING: "[ ]",
        TaskStatus.IN_PROGRESS: "[>]",
        TaskStatus.COMPLETED: "[x]",
    }
    icon = status_icon.get(task.status, "[ ]")
    blocked = f" (blocked by: {', '.join(task.blocked_by)})" if task.blocked_by else ""
    return f"#{task.id} {icon} {task.subject}{blocked}"


def _format_task_detail(task: Task) -> str:
    """Format a task with full details."""
    lines = [
        f"Task #{task.id}: {task.subject}",
        f"Status: {task.status.value}",
        f"Description: {task.description}",
    ]
    if task.active_form:
        lines.append(f"Active form: {task.active_form}")
    if task.blocks:
        lines.append(f"Blocks: {', '.join(task.blocks)}")
    if task.blocked_by:
        lines.append(f"Blocked by: {', '.join(task.blocked_by)}")
    if task.metadata:
        lines.append(f"Metadata: {json.dumps(dict(task.metadata))}")
    return "\n".join(lines)


async def _task_create_handler(
    subject: str,
    description: str,
    active_form: str = "",
    *,
    _store: TaskStore,
) -> str:
    """Create a new task."""
    if not subject.strip():
        return "Error: Subject must not be empty"

    task = await _store.create(
        subject=subject,
        description=description,
        active_form=active_form,
    )
    logger.info(f"[TaskCreate] #{task.id}: {subject}")
    return f"Created task #{task.id}: {subject}"


async def _task_get_handler(
    task_id: str,
    *,
    _store: TaskStore,
) -> str:
    """Get a task by ID."""
    task = await _store.get(task_id)
    if task is None:
        return f"Error: Task #{task_id} not found"
    return _format_task_detail(task)


async def _task_list_handler(
    *,
    _store: TaskStore,
) -> str:
    """List all tasks."""
    tasks = await _store.list_all()
    if not tasks:
        return "No tasks created yet."

    pending = [t for t in tasks if t.status == TaskStatus.PENDING]
    in_progress = [t for t in tasks if t.status == TaskStatus.IN_PROGRESS]
    completed = [t for t in tasks if t.status == TaskStatus.COMPLETED]

    lines: list[str] = []
    if in_progress:
        lines.append("In Progress:")
        lines.extend(f"  {_format_task(t)}" for t in in_progress)
    if pending:
        lines.append("Pending:")
        lines.extend(f"  {_format_task(t)}" for t in pending)
    if completed:
        lines.append("Completed:")
        lines.extend(f"  {_format_task(t)}" for t in completed)

    summary = f"Total: {len(tasks)} ({len(completed)} done, {len(in_progress)} active, {len(pending)} pending)"
    lines.append(f"\n{summary}")
    return "\n".join(lines)


async def _task_update_handler(
    task_id: str,
    status: str = "",
    subject: str = "",
    description: str = "",
    active_form: str = "",
    add_blocks: str = "",
    add_blocked_by: str = "",
    *,
    _store: TaskStore,
) -> str:
    """Update a task."""
    # Handle deletion
    if status == "deleted":
        deleted = await _store.delete(task_id)
        if not deleted:
            return f"Error: Task #{task_id} not found"
        return f"Deleted task #{task_id}"

    # Parse status
    parsed_status: TaskStatus | None = None
    if status:
        try:
            parsed_status = TaskStatus(status)
        except ValueError:
            return f"Error: Invalid status '{status}'. Use: pending, in_progress, completed, deleted"

    # Parse dependency lists (comma-separated IDs)
    blocks_list = [s.strip() for s in add_blocks.split(",") if s.strip()] if add_blocks else []
    blocked_by_list = [s.strip() for s in add_blocked_by.split(",") if s.strip()] if add_blocked_by else []

    updated = await _store.update(
        task_id,
        status=parsed_status,
        subject=subject or None,
        description=description or None,
        active_form=active_form or None,
        add_blocks=blocks_list,
        add_blocked_by=blocked_by_list,
    )

    if updated is None:
        return f"Error: Task #{task_id} not found"

    logger.info(f"[TaskUpdate] #{task_id}: status={updated.status.value}")
    return f"Updated task #{task_id}\n{_format_task(updated)}"


def create_task_tools(store: TaskStore | None = None) -> tuple[Tool, ...]:
    """
    Create the task management tool set.

    All tools share the same TaskStore instance.
    If no store is provided, a new one is created.
    """
    task_store = store or TaskStore()

    task_create = Tool(
        name="task_create",
        description=(
            "Create a task to track a step in your work. "
            "Use this to break down complex tasks into tracked steps."
        ),
        handler=lambda subject, description="", active_form="": _task_create_handler(
            subject, description, active_form, _store=task_store,
        ),
        parameters={
            "subject": {
                "type": "string",
                "description": "Brief title for the task (imperative form, e.g., 'Fix authentication bug')",
            },
            "description": {
                "type": "string",
                "description": "Detailed description of what needs to be done",
            },
            "active_form": {
                "type": "string",
                "description": "Present continuous form for spinner (e.g., 'Fixing authentication bug')",
            },
        },
        required_params=("subject",),
        tool_type=ToolType.WRITE,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
        tags=frozenset({"task", "create", "tracking", "todo"}),
    )

    task_get = Tool(
        name="task_get",
        description=(
            "Get full details of a task by its ID. "
            "Shows description, status, dependencies, and metadata."
        ),
        handler=lambda task_id: _task_get_handler(task_id, _store=task_store),
        parameters={
            "task_id": {
                "type": "string",
                "description": "The task ID (e.g., '1', '2')",
            },
        },
        required_params=("task_id",),
        tool_type=ToolType.READ,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
        tags=frozenset({"task", "get", "tracking"}),
    )

    task_list = Tool(
        name="task_list",
        description=(
            "List all tasks grouped by status (in_progress, pending, completed). "
            "Shows progress summary."
        ),
        handler=lambda: _task_list_handler(_store=task_store),
        parameters={},
        required_params=(),
        tool_type=ToolType.READ,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
        tags=frozenset({"task", "list", "tracking"}),
    )

    task_update = Tool(
        name="task_update",
        description=(
            "Update a task's status, subject, or dependencies. "
            "Set status to 'in_progress' when starting, 'completed' when done, "
            "'deleted' to remove. Use add_blocks/add_blocked_by for dependencies."
        ),
        handler=lambda task_id, status="", subject="", description="",
                       active_form="", add_blocks="", add_blocked_by="": _task_update_handler(
            task_id, status, subject, description, active_form,
            add_blocks, add_blocked_by, _store=task_store,
        ),
        parameters={
            "task_id": {
                "type": "string",
                "description": "The task ID to update",
            },
            "status": {
                "type": "string",
                "description": "New status: pending, in_progress, completed, or deleted",
            },
            "subject": {
                "type": "string",
                "description": "New subject (leave empty to keep current)",
            },
            "description": {
                "type": "string",
                "description": "New description (leave empty to keep current)",
            },
            "active_form": {
                "type": "string",
                "description": "Present continuous form for spinner display",
            },
            "add_blocks": {
                "type": "string",
                "description": "Comma-separated task IDs that this task blocks",
            },
            "add_blocked_by": {
                "type": "string",
                "description": "Comma-separated task IDs that block this task",
            },
        },
        required_params=("task_id",),
        tool_type=ToolType.WRITE,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
        tags=frozenset({"task", "update", "tracking", "status"}),
    )

    return (task_create, task_get, task_list, task_update)
