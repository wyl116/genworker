"""Runtime debug routes."""
from __future__ import annotations

from fastapi import APIRouter, Request

from src.api.auth import require_api_auth
from src.api.routes.health_routes import _probe_langgraph_health
from src.common.settings import get_settings
from src.runtime.runtime_views import (
    aggregate_readiness,
    build_dependency_statuses,
    resolve_worker_loaded,
    runtime_component_requirements,
    snapshot_runtime_components,
)

router = APIRouter(prefix="/api/v1/debug", tags=["runtime"])


@router.get("/runtime")
async def runtime_debug(request: Request) -> dict:
    """Return a lightweight runtime foundation snapshot for debugging."""
    require_api_auth(request)

    settings = get_settings()
    app_state = request.app.state
    bootstrap_context = getattr(app_state, "bootstrap_context", None)
    tenant_id = (
        bootstrap_context.get_state("tenant_id", "demo")
        if bootstrap_context is not None and hasattr(bootstrap_context, "get_state")
        else "demo"
    )
    components = snapshot_runtime_components(app_state)
    langgraph_probe = await _probe_langgraph_health(app_state)
    dependencies = build_dependency_statuses(
        settings=settings,
        components=components,
        langgraph_probe=langgraph_probe,
    )
    readiness = aggregate_readiness(
        runtime_profile=getattr(settings, "runtime_profile", "local"),
        worker_loaded=resolve_worker_loaded(app_state),
        model_ready=bool(getattr(bootstrap_context, "llm_ready", False)),
        components=components,
        requirements=runtime_component_requirements(app_state),
        dependencies=dependencies,
        langgraph_probe=langgraph_probe,
    )
    resolver = getattr(app_state, "resolve_default_worker", None)
    selection = resolver() if callable(resolver) else None
    return {
        "status": readiness["status"],
        "runtime_profile": readiness["runtime_profile"],
        "tenant_id": tenant_id,
        "default_worker_id": getattr(selection, "worker_id", ""),
        "worker_loaded": readiness["worker_loaded"],
        "model_ready": readiness["model_ready"],
        "dependencies": readiness["dependencies"],
        "blocking_reasons": readiness["blocking_reasons"],
        "warnings": readiness["warnings"],
        "components": {
            name: component.to_public_dict()
            for name, component in components.items()
        },
    }
