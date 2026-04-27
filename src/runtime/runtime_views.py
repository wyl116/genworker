from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.common.runtime_status import ComponentRuntimeStatus, ComponentStatus


def snapshot_runtime_components(app_state: Any) -> dict[str, ComponentRuntimeStatus]:
    """Read the registered runtime component snapshot from app.state."""
    snapshotter = getattr(app_state, "snapshot_runtime_components", None)
    if callable(snapshotter):
        result = snapshotter()
        if isinstance(result, dict):
            return result
    return {}


def runtime_component_requirements(app_state: Any) -> dict[str, bool]:
    """Read requirement flags for registered runtime components."""
    raw = getattr(app_state, "runtime_component_requirements", None)
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def resolve_worker_loaded(app_state: Any) -> bool:
    """Return whether at least one default-chat-capable worker is loaded."""
    worker_registry = getattr(app_state, "worker_registry", None)
    if worker_registry is None:
        return False
    count_loaded = getattr(worker_registry, "count_loaded", None)
    if callable(count_loaded):
        return count_loaded() > 0
    list_all = getattr(worker_registry, "list_all", None)
    if callable(list_all):
        return len(tuple(list_all())) > 0
    resolver = getattr(app_state, "resolve_default_worker", None)
    if callable(resolver):
        try:
            selection = resolver()
            return bool(getattr(selection, "worker_loaded", False))
        except Exception:
            pass
    return False


def build_dependency_statuses(
    *,
    settings: Any,
    components: Mapping[str, ComponentRuntimeStatus],
    langgraph_probe: dict[str, bool],
) -> dict[str, str]:
    """Build public dependency readiness from runtime component state."""
    dependencies: dict[str, str] = {}
    toggles = {
        "redis": bool(getattr(settings, "redis_enabled", False)),
        "mysql": bool(getattr(settings, "mysql_enabled", False)),
        "openviking": bool(getattr(settings, "openviking_enabled", False)),
    }
    for name, enabled in toggles.items():
        status = components.get(name)
        if status is not None:
            dependencies[name] = status.status.value
        else:
            dependencies[name] = (
                ComponentStatus.FAILED.value if enabled else ComponentStatus.DISABLED.value
            )
    dependencies["langgraph"] = (
        ComponentStatus.READY.value
        if langgraph_probe.get("import_ok") and langgraph_probe.get("checkpointer_ok")
        else ComponentStatus.FAILED.value
    )
    return dependencies


def aggregate_readiness(
    *,
    runtime_profile: str,
    worker_loaded: bool,
    model_ready: bool,
    components: Mapping[str, ComponentRuntimeStatus],
    requirements: Mapping[str, bool],
    dependencies: Mapping[str, str],
    langgraph_probe: Mapping[str, bool],
) -> dict[str, Any]:
    """Aggregate component state into a single readiness payload."""
    blocking_reasons: list[str] = []
    warnings: list[str] = []

    if not worker_loaded:
        blocking_reasons.append("worker_not_loaded")
    if not model_ready:
        blocking_reasons.append("model_not_ready")
    if not langgraph_probe.get("import_ok", False):
        blocking_reasons.append("langgraph_import_unavailable")
    if not langgraph_probe.get("checkpointer_ok", False):
        blocking_reasons.append("langgraph_checkpointer_unavailable")

    for name, component in components.items():
        required = bool(requirements.get(name, False))
        if component.status == ComponentStatus.READY:
            continue
        if component.status == ComponentStatus.DISABLED:
            if required:
                blocking_reasons.append(f"{name}_disabled")
            continue

        reason = _format_component_reason(component)
        if component.status == ComponentStatus.FAILED and required:
            blocking_reasons.append(reason)
            continue
        warnings.append(reason)

    status = ComponentStatus.READY.value
    if blocking_reasons:
        status = ComponentStatus.FAILED.value
    elif warnings:
        status = ComponentStatus.DEGRADED.value

    return {
        "status": status,
        "runtime_profile": runtime_profile,
        "worker_loaded": worker_loaded,
        "model_ready": model_ready,
        "dependencies": dict(dependencies),
        "blocking_reasons": blocking_reasons,
        "warnings": warnings,
    }


def _format_component_reason(component: ComponentRuntimeStatus) -> str:
    if component.status == ComponentStatus.DEGRADED:
        if (
            component.fallback_backend
            and component.selected_backend == component.fallback_backend
        ):
            base = (
                f"{component.component} fell back to {component.selected_backend}"
            )
        else:
            base = f"{component.component} degraded"
    elif component.status == ComponentStatus.FAILED:
        base = f"{component.component} failed"
    else:
        base = f"{component.component} disabled"

    if component.last_error:
        return f"{base}: {component.last_error.splitlines()[0][:200]}"
    return base
