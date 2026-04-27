"""
Task store - In-memory task tracking for Worker self-management.

Provides lightweight step tracking within a single ReAct loop.
Tasks live only for the duration of one execution run.
Thread-safe via asyncio.Lock for concurrent tool calls.

Data model inspired by claude-code's TodoV2 system but simplified
for single-Worker, single-run lifecycle.
"""
import asyncio
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Mapping, Sequence


class TaskStatus(str, Enum):
    """Task lifecycle states."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass(frozen=True)
class Task:
    """Immutable task record."""
    id: str
    subject: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    active_form: str = ""
    blocks: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def is_blocked(self) -> bool:
        """Check if task has unresolved blockers."""
        return len(self.blocked_by) > 0


class TaskStore:
    """
    In-memory task store for a single execution run.

    Not persisted across runs. Each ReactEngine execution
    creates a fresh store via create_task_tools().
    """

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._next_id: int = 1
        self._lock = asyncio.Lock()

    async def create(
        self,
        subject: str,
        description: str,
        active_form: str = "",
        metadata: Mapping[str, object] | None = None,
    ) -> Task:
        """Create a new task. Returns the created Task."""
        async with self._lock:
            task_id = str(self._next_id)
            self._next_id += 1
            task = Task(
                id=task_id,
                subject=subject,
                description=description,
                active_form=active_form,
                metadata=metadata or {},
            )
            self._tasks[task_id] = task
            return task

    async def get(self, task_id: str) -> Task | None:
        """Get a task by ID."""
        return self._tasks.get(task_id)

    async def list_all(self) -> tuple[Task, ...]:
        """List all tasks."""
        return tuple(self._tasks.values())

    async def update(
        self,
        task_id: str,
        *,
        status: TaskStatus | None = None,
        subject: str | None = None,
        description: str | None = None,
        active_form: str | None = None,
        add_blocks: Sequence[str] = (),
        add_blocked_by: Sequence[str] = (),
        metadata_merge: Mapping[str, object | None] | None = None,
    ) -> Task | None:
        """
        Update a task. Returns updated Task or None if not found.

        Uses dataclasses.replace() for immutability.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None

            updates: dict = {}

            if status is not None:
                updates["status"] = status
            if subject is not None:
                updates["subject"] = subject
            if description is not None:
                updates["description"] = description
            if active_form is not None:
                updates["active_form"] = active_form

            if add_blocks:
                new_blocks = tuple(
                    b for b in (*task.blocks, *add_blocks)
                    if b not in task.blocks or b in add_blocks
                )
                # Deduplicate while preserving order
                seen: set[str] = set()
                deduped: list[str] = []
                for b in new_blocks:
                    if b not in seen:
                        seen.add(b)
                        deduped.append(b)
                updates["blocks"] = tuple(deduped)

            if add_blocked_by:
                existing = set(task.blocked_by)
                new_items = tuple(b for b in add_blocked_by if b not in existing)
                updates["blocked_by"] = task.blocked_by + new_items

            if metadata_merge:
                merged = dict(task.metadata)
                for k, v in metadata_merge.items():
                    if v is None:
                        merged.pop(k, None)
                    else:
                        merged[k] = v
                updates["metadata"] = merged

            updated = replace(task, **updates) if updates else task
            self._tasks[task_id] = updated

            # If completing a task, remove it from other tasks' blocked_by
            if status == TaskStatus.COMPLETED:
                self._cascade_unblock(task_id)

            return updated

    def _cascade_unblock(self, completed_id: str) -> None:
        """Remove completed task from other tasks' blocked_by lists."""
        for tid, task in self._tasks.items():
            if completed_id in task.blocked_by:
                new_blocked = tuple(
                    b for b in task.blocked_by if b != completed_id
                )
                self._tasks[tid] = replace(task, blocked_by=new_blocked)

    async def delete(self, task_id: str) -> bool:
        """Delete a task. Returns True if found and deleted."""
        async with self._lock:
            if task_id not in self._tasks:
                return False

            del self._tasks[task_id]

            # Remove references from other tasks
            for tid, task in list(self._tasks.items()):
                changed = False
                new_blocks = task.blocks
                new_blocked_by = task.blocked_by

                if task_id in task.blocks:
                    new_blocks = tuple(b for b in task.blocks if b != task_id)
                    changed = True
                if task_id in task.blocked_by:
                    new_blocked_by = tuple(b for b in task.blocked_by if b != task_id)
                    changed = True

                if changed:
                    self._tasks[tid] = replace(
                        task, blocks=new_blocks, blocked_by=new_blocked_by,
                    )

            return True
