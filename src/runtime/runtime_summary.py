"""Runtime startup summary helpers."""
from __future__ import annotations

from typing import Any

from src.runtime.runtime_views import snapshot_runtime_components


def build_runtime_summary(app_state: Any) -> str:
    """Build one compact startup summary line for runtime foundations."""
    components = snapshot_runtime_components(app_state)
    resolver = getattr(app_state, "resolve_default_worker", None)
    selection = resolver() if callable(resolver) else None
    default_worker = getattr(selection, "worker_id", "") or "-"
    worker_loaded = bool(getattr(selection, "worker_loaded", False))
    runtime_profile = getattr(app_state, "runtime_profile", "local")
    settings = getattr(app_state, "settings", None)
    redis_enabled = bool(getattr(settings, "redis_enabled", False))
    mysql_enabled = bool(getattr(settings, "mysql_enabled", False))
    openviking_enabled = bool(getattr(settings, "openviking_enabled", False))
    im_channel_enabled = bool(getattr(settings, "im_channel_enabled", False))

    return (
        "[Runtime] "
        f"profile={runtime_profile} "
        f"worker_loaded={str(worker_loaded).lower()} "
        f"default_worker={default_worker} "
        f"redis_enabled={str(redis_enabled).lower()} "
        f"mysql_enabled={str(mysql_enabled).lower()} "
        f"openviking_enabled={str(openviking_enabled).lower()} "
        f"im_channel_enabled={str(im_channel_enabled).lower()} "
        f"redis={_status_value(components, 'redis')} "
        f"mysql={_status_value(components, 'mysql')} "
        f"openviking={_status_value(components, 'openviking')} "
        f"session_store={_backend_value(components, 'session_store')} "
        f"inbox_store={_backend_value(components, 'inbox_store')} "
        f"message_dedup={_backend_value(components, 'message_dedup')} "
        f"dead_letter_store={_backend_value(components, 'dead_letter_store')} "
        f"main_session_meta={_backend_value(components, 'main_session_meta')} "
        f"attention_ledger={_backend_value(components, 'attention_ledger')}"
    )


def _status_value(components: dict[str, Any], name: str) -> str:
    component = components.get(name)
    if component is None:
        return "unknown"
    return getattr(component.status, "value", str(component.status))


def _backend_value(components: dict[str, Any], name: str) -> str:
    component = components.get(name)
    if component is None:
        return "unknown"
    return str(getattr(component, "selected_backend", "unknown") or "unknown")
