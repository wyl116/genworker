"""
Health check routes.

Provides a GET /health endpoint that returns server status.
"""
import importlib
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request

from src.common.settings import get_settings
from src.runtime.runtime_views import (
    aggregate_readiness,
    build_dependency_statuses,
    resolve_worker_loaded,
    runtime_component_requirements,
    snapshot_runtime_components,
)

router = APIRouter()

_start_time = time.monotonic()


async def _probe_langgraph_health(app_state: Any) -> dict[str, bool]:
    """Probe langgraph importability and checkpointer availability at request time."""
    import_ok = False
    checkpointer_ok = False

    try:
        importlib.import_module("langgraph")
        import_ok = True
    except Exception:
        import_ok = False

    checkpointer = getattr(app_state, "langgraph_checkpointer", None)
    if checkpointer is not None:
        try:
            await checkpointer.aget_tuple({"configurable": {"thread_id": "__healthcheck__"}})
            checkpointer_ok = True
        except Exception:
            checkpointer_ok = False

    return {
        "import_ok": import_ok,
        "checkpointer_ok": checkpointer_ok,
    }


@router.get("/health")
async def health_check(request: Request):
    """
    Health check endpoint.

    Returns:
        JSON with status, service info, and uptime.
    """
    settings = get_settings()
    uptime_seconds = time.monotonic() - _start_time
    engine_registry = dict(getattr(request.app.state, "engine_registry", {}) or {})
    engine_registry["langgraph"] = await _probe_langgraph_health(request.app.state)

    return {
        "status": "healthy",
        "service": settings.service_name,
        "version": settings.service_version,
        "environment": settings.environment,
        "uptime_seconds": round(uptime_seconds, 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "engines": engine_registry,
    }


@router.get("/readiness")
async def readiness_check(request: Request):
    """Readiness endpoint for the default chat execution path."""
    settings = get_settings()
    app_state = request.app.state
    components = snapshot_runtime_components(app_state)
    langgraph_probe = await _probe_langgraph_health(app_state)
    dependencies = build_dependency_statuses(
        settings=settings,
        components=components,
        langgraph_probe=langgraph_probe,
    )
    bootstrap_context = getattr(app_state, "bootstrap_context", None)
    payload = aggregate_readiness(
        runtime_profile=getattr(settings, "runtime_profile", "local"),
        worker_loaded=resolve_worker_loaded(app_state),
        model_ready=bool(getattr(bootstrap_context, "llm_ready", False)),
        components=components,
        requirements=runtime_component_requirements(app_state),
        dependencies=dependencies,
        langgraph_probe=langgraph_probe,
    )
    return payload
