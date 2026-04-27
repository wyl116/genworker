"""
WorkerRegistry - frozen registry for worker lookup and matching.

Workers are matched by scanning their associated SkillRegistry's
keyword sets against the incoming task description.
"""
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from src.common.logger import get_logger
from src.skills.registry import SkillRegistry

from .models import Worker

logger = get_logger()


@dataclass(frozen=True)
class WorkerEntry:
    """A worker with its associated skill registry."""
    worker: Worker
    skill_registry: SkillRegistry


@dataclass(frozen=True)
class WorkerRegistry:
    """
    Immutable registry of workers with skill-based matching.

    Use build_worker_registry() factory to construct instances.
    """
    _entries: Mapping[str, WorkerEntry] = field(default_factory=dict)
    _default_worker_id: str = ""

    def get(self, worker_id: str) -> Optional[WorkerEntry]:
        """Get a worker entry by ID."""
        return self._entries.get(worker_id)

    def list_all(self) -> tuple[WorkerEntry, ...]:
        """List all registered worker entries."""
        return tuple(self._entries.values())

    def get_default(self) -> Optional[WorkerEntry]:
        """Get the default worker entry, if configured."""
        if self._default_worker_id:
            return self._entries.get(self._default_worker_id)
        return None

    def count_loaded(self) -> int:
        """Return the number of loaded workers in the registry."""
        return len(self._entries)

    def match(self, task_description: str) -> Optional[WorkerEntry]:
        """
        Match a task to the best worker by scanning skill keywords.

        Iterates each worker's skill registry, computes keyword score
        per skill, and returns the worker with the highest aggregate.
        Falls back to default worker if no match.

        Args:
            task_description: The user's task text.

        Returns:
            Best matching WorkerEntry, or None.
        """
        if not task_description.strip():
            return self.get_default()

        task_lower = task_description.lower()
        best_entry: Optional[WorkerEntry] = None
        best_score = 0.0

        for entry in self._entries.values():
            score = _compute_worker_score(task_lower, entry.skill_registry)
            if score > best_score:
                best_score = score
                best_entry = entry

        if best_entry is not None and best_score > 0:
            logger.info(
                f"[WorkerRegistry] Matched worker "
                f"'{best_entry.worker.worker_id}' (score={best_score:.2f})"
            )
            return best_entry

        default = self.get_default()
        if default is not None:
            logger.info(
                f"[WorkerRegistry] No match; using default worker "
                f"'{default.worker.worker_id}'"
            )
        return default

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, worker_id: str) -> bool:
        return worker_id in self._entries


def build_worker_registry(
    entries: Sequence[WorkerEntry],
    default_worker_id: str = "",
) -> WorkerRegistry:
    """
    Factory function to build a frozen WorkerRegistry.

    Args:
        entries: Sequence of WorkerEntry objects.
        default_worker_id: ID of the default worker.

    Returns:
        Frozen WorkerRegistry.
    """
    entries_map: dict[str, WorkerEntry] = {}
    for entry in entries:
        wid = entry.worker.worker_id
        entries_map[wid] = entry

    logger.info(
        f"[WorkerRegistry] Built registry with {len(entries_map)} worker(s)"
    )
    return WorkerRegistry(
        _entries=entries_map,
        _default_worker_id=default_worker_id,
    )


def replace_worker_entry(
    registry: WorkerRegistry,
    entry: WorkerEntry,
) -> WorkerRegistry:
    """Return a new registry with one worker entry replaced."""
    entries = dict(registry._entries)
    entries[entry.worker.worker_id] = entry
    return WorkerRegistry(
        _entries=entries,
        _default_worker_id=registry._default_worker_id,
    )


def _compute_worker_score(
    task_lower: str,
    skill_registry: SkillRegistry,
) -> float:
    """Compute aggregate keyword score across all skills in the registry."""
    total = 0.0
    for skill in skill_registry.list_all():
        for kw in skill.keywords:
            if kw.keyword.lower() in task_lower:
                total += kw.weight
    return total
