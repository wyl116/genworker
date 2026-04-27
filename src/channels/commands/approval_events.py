"""Approval-capable inbox event types shared by command handlers."""
from __future__ import annotations

from collections.abc import Iterator, Set as AbstractSet

from src.worker.lifecycle.task_confirmation import CONFIRMATION_EVENT_TYPE

LANGGRAPH_INTERRUPT_EVENT_TYPE = "langgraph.interrupt"

_extra_approval_event_types: set[str] = set()


def register_approval_event_type(event_type: str) -> None:
    """Register an extra inbox event type accepted by approval commands."""
    normalized = str(event_type or "").strip()
    if normalized:
        _extra_approval_event_types.add(normalized)


def approval_event_types() -> frozenset[str]:
    """Return the current approval whitelist."""
    return frozenset(
        {
            CONFIRMATION_EVENT_TYPE,
            LANGGRAPH_INTERRUPT_EVENT_TYPE,
            *_extra_approval_event_types,
        }
    )


class _ApprovalEventTypesView(AbstractSet[str]):
    """Live set-like view over the dynamic approval whitelist."""

    def __contains__(self, value: object) -> bool:
        return value in approval_event_types()

    def __iter__(self) -> Iterator[str]:
        return iter(approval_event_types())

    def __len__(self) -> int:
        return len(approval_event_types())

    def __repr__(self) -> str:
        return repr(approval_event_types())


APPROVAL_EVENT_TYPES = _ApprovalEventTypesView()
