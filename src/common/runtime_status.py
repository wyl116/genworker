from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ComponentStatus(str, Enum):
    """Unified runtime status for pluggable components."""

    DISABLED = "disabled"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True)
class ComponentRuntimeStatus:
    """Runtime backend selection and health snapshot for one component."""

    component: str
    enabled: bool
    status: ComponentStatus
    selected_backend: str
    primary_backend: Optional[str] = None
    fallback_backend: Optional[str] = None
    ground_truth: Optional[str] = None
    last_error: str = ""

    def to_public_dict(self) -> dict:
        """Serialize a safe, single-line public view for routes."""
        error = self.last_error
        if error:
            error = error.splitlines()[0][:200]
        return {
            "component": self.component,
            "enabled": self.enabled,
            "status": self.status.value,
            "selected_backend": self.selected_backend,
            "primary_backend": self.primary_backend,
            "fallback_backend": self.fallback_backend,
            "ground_truth": self.ground_truth,
            "last_error": error,
        }


def aggregate_component_statuses(
    component: str,
    statuses: list[ComponentRuntimeStatus] | tuple[ComponentRuntimeStatus, ...],
) -> ComponentRuntimeStatus:
    """Collapse per-instance runtime snapshots into one component-level view."""
    if not statuses:
        return ComponentRuntimeStatus(
            component=component,
            enabled=False,
            status=ComponentStatus.DISABLED,
            selected_backend="disabled",
        )

    if any(item.status == ComponentStatus.FAILED for item in statuses):
        status = ComponentStatus.FAILED
    elif any(item.status == ComponentStatus.DEGRADED for item in statuses):
        status = ComponentStatus.DEGRADED
    elif all(item.status == ComponentStatus.DISABLED for item in statuses):
        status = ComponentStatus.DISABLED
    else:
        status = ComponentStatus.READY

    return ComponentRuntimeStatus(
        component=component,
        enabled=any(item.enabled for item in statuses),
        status=status,
        selected_backend=_merge_runtime_field(statuses, "selected_backend", default="unknown"),
        primary_backend=_merge_runtime_field(statuses, "primary_backend"),
        fallback_backend=_merge_runtime_field(statuses, "fallback_backend"),
        ground_truth=_merge_runtime_field(statuses, "ground_truth"),
        last_error=next(
            (
                str(item.last_error).splitlines()[0][:200]
                for item in statuses
                if item.last_error
            ),
            "",
        ),
    )


def _merge_runtime_field(
    statuses: list[ComponentRuntimeStatus] | tuple[ComponentRuntimeStatus, ...],
    field_name: str,
    *,
    default: str | None = None,
) -> str | None:
    values = {
        str(value).strip()
        for item in statuses
        if (value := getattr(item, field_name, None))
        and str(value).strip()
    }
    if not values:
        return default
    if len(values) == 1:
        return next(iter(values))
    return "mixed"
