"""
Worker task streaming and worker ops/config routes.

POST /api/v1/worker/task/stream - SSE streaming endpoint that:
1. Validates request via Pydantic (422 on missing fields)
2. Creates WorkerRouter from app.state dependencies
3. Streams AG-UI SSE events from route_stream()
4. Returns ERROR SSE events for domain errors (not HTTP error codes)
"""
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, Query, Request
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from src.api.auth import enforce_worker_scope, require_api_auth
from src.api.models.request_models import WorkerTaskRequest
from src.api.service_ingress import (
    build_service_queued_error,
    merge_session_metadata,
    prepare_service_ingress,
)
from src.common.exceptions import ConfigException
from src.common.logger import get_logger
from src.common.paths import resolve_workspace_root
from src.streaming.event_adapter import create_sse_formatter
from src.streaming.events import ErrorEvent, StreamEvent
from src.worker.router import WorkerRouter

logger = get_logger()

router = APIRouter(prefix="/api/v1/worker", tags=["worker"])


async def _generate_sse(
    request: Request,
    worker_router: WorkerRouter,
    task: str,
    tenant_id: str,
    worker_id: str | None,
    thread_id: str | None = None,
    channel_type: str | None = None,
    channel_id: str | None = None,
    display_name: str | None = None,
    topic: str | None = None,
    metadata: dict[str, str] | None = None,
    protocol: str = "ag-ui",
) -> AsyncGenerator[str, None]:
    """
    Async generator that yields SSE-formatted lines from WorkerRouter.

    Catches unexpected exceptions and yields an ERROR event
    so the client always gets a well-formed SSE stream.
    """
    metadata = metadata or {}
    formatter = create_sse_formatter(protocol=protocol, thread_id=thread_id)
    try:
        session = None
        assistant_parts: list[str] = []
        spawned_task_ids: list[str] = []
        try:
            resolved_entry = worker_router.resolve_entry(
                task=task,
                tenant_id=tenant_id,
                worker_id=worker_id,
            )
        except ConfigException as exc:
            error_event = ErrorEvent(
                run_id="",
                code="TENANT_NOT_FOUND",
                message=f"Tenant '{tenant_id}' not found: {exc}",
            )
            for line in formatter.format(error_event):
                yield line
            return
        resolved_worker_id = worker_id or (
            resolved_entry.worker.worker_id if resolved_entry is not None else None
        )
        task_context = ""

        if resolved_entry is not None and resolved_entry.worker.is_service:
            session_manager = getattr(request.app.state, "session_manager", None)
            if session_manager is None:
                error_event = ErrorEvent(
                    run_id="",
                    code="CONVERSATION_NOT_INITIALIZED",
                    message="Conversation subsystem not initialized",
                )
                for line in formatter.format(error_event):
                    yield line
                return

            service_state = await prepare_service_ingress(
                session_manager=session_manager,
                worker_router=worker_router,
                worker=resolved_entry.worker,
                tenant_id=tenant_id,
                message=task,
                thread_id=thread_id,
                default_channel_type="task",
                default_channel_id="",
                channel_type=channel_type,
                channel_id=channel_id,
                display_name=display_name,
                topic=topic,
                metadata=metadata,
            )
            if service_state.queued_position is not None:
                for line in formatter.format(
                    build_service_queued_error(service_state.queued_position)
                ):
                    yield line
                return
            formatter = create_sse_formatter(
                protocol=protocol,
                thread_id=service_state.thread_id,
            )
            task_context = service_state.task_context

            session = await session_manager.get_or_create(
                thread_id=service_state.thread_id,
                tenant_id=tenant_id,
                worker_id=resolved_entry.worker.worker_id,
                ttl_seconds=service_state.session_ttl,
                metadata=service_state.session_metadata,
            )
            if task_context:
                from dataclasses import replace

                session = replace(
                    session,
                    metadata=merge_session_metadata(
                        session.metadata, {"service_profile_context": task_context},
                    ),
                )
            await session_manager.save(session)

        async for event in worker_router.route_stream(
            task=task,
            tenant_id=tenant_id,
            worker_id=resolved_worker_id,
            task_context=task_context,
            conversation_session=session,
        ):
            event_type = getattr(event, "event_type", "")
            if event_type == "TEXT_MESSAGE":
                content = getattr(event, "content", "")
                if content:
                    assistant_parts.append(content)
            elif event_type == "TASK_SPAWNED":
                task_id = getattr(event, "task_id", "")
                if task_id:
                    spawned_task_ids.append(task_id)
            for line in formatter.format(event):
                yield line
        if session is not None:
            from src.conversation.models import ChatMessage

            if assistant_parts:
                session = session.append_message(ChatMessage(
                    role="assistant",
                    content="\n\n".join(
                        part.strip() for part in assistant_parts if str(part).strip()
                    ),
                ))
            for task_id in spawned_task_ids:
                session = session.add_spawned_task(task_id)
            await session_manager.save(session)
    except Exception as exc:
        logger.error(f"[worker_routes] Stream error: {exc}", exc_info=True)
        error_event = ErrorEvent(
            run_id="",
            code="STREAM_ERROR",
            message=str(exc),
        )
        for line in formatter.format(error_event):
            yield line


@router.post("/task/stream")
async def stream_task(
    request_body: WorkerTaskRequest,
    request: Request,
    protocol: str = Query(default="ag-ui", pattern="^(ag-ui|legacy)$"),
) -> StreamingResponse:
    """
    Stream a worker task execution as Server-Sent Events.

    The endpoint always returns 200 with text/event-stream content type.
    Domain errors (TENANT_NOT_FOUND, WORKER_NOT_FOUND, SKILL_NOT_FOUND)
    are delivered as ERROR SSE events within the stream.

    Only Pydantic validation errors produce HTTP 422 (handled by FastAPI).

    Args:
        request_body: Validated WorkerTaskRequest.
        request: FastAPI Request for accessing app.state.

    Returns:
        StreamingResponse with SSE content.
    """
    require_api_auth(request)
    if request_body.worker_id:
        enforce_worker_scope(request, request_body.worker_id)

    app = request.app
    worker_router: WorkerRouter = app.state.worker_router

    logger.info(
        f"[worker_routes] Streaming task: tenant={request_body.tenant_id}, "
        f"worker={request_body.worker_id}, task={request_body.task[:80]}"
    )

    return StreamingResponse(
        _generate_sse(
            request=request,
            worker_router=worker_router,
            task=request_body.task,
            tenant_id=request_body.tenant_id,
            worker_id=request_body.worker_id,
            thread_id=request_body.thread_id,
            channel_type=request_body.channel_type,
            channel_id=request_body.channel_id,
            display_name=request_body.display_name,
            topic=request_body.topic,
            metadata=request_body.metadata,
            protocol=protocol,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/ops/overview")
async def worker_ops_overview(
    request: Request,
    tenant_id: str = Query(default="demo"),
) -> dict:
    """Return per-worker operational state for backend-online introspection."""
    require_api_auth(request)

    app = request.app
    worker_registry = getattr(app.state, "worker_registry", None)
    trigger_managers = getattr(app.state, "trigger_managers", {}) or {}
    worker_schedulers = getattr(app.state, "worker_schedulers", {}) or {}
    sensor_registries = getattr(app.state, "sensor_registries", {}) or {}
    worker_reload_status = getattr(app.state, "worker_reload_status", {}) or {}
    workspace_root = resolve_workspace_root(getattr(app.state, "workspace_root", None))
    persona_reload = _persona_reload_snapshot(app)
    if worker_registry is None:
        return {
            "tenant_id": tenant_id,
            "worker_count": 0,
            "workers": [],
            "persona_reload": persona_reload,
        }

    workers: list[dict] = []
    for entry in worker_registry.list_all():
        worker = entry.worker
        worker_dir = (
            workspace_root / "tenants" / tenant_id / "workers" / worker.worker_id
        )
        trigger_manager = trigger_managers.get(worker.worker_id)
        worker_scheduler = worker_schedulers.get(worker.worker_id)
        sensor_registry = sensor_registries.get(worker.worker_id)

        trigger_snapshot = (
            trigger_manager.registration_snapshot
            if trigger_manager is not None
            and hasattr(trigger_manager, "registration_snapshot")
            else {"duty_count": 0, "resource_count": 0, "duties": {}}
        )
        sensor_snapshot = (
            sensor_registry.health
            if sensor_registry is not None
            and hasattr(sensor_registry, "health")
            else {"sensor_count": 0, "sensors": {}}
        )

        workers.append({
            "worker_id": worker.worker_id,
            "name": worker.name,
            "backend_online": bool(
                worker_scheduler is not None
                or trigger_snapshot.get("resource_count", 0) > 0
                or sensor_snapshot.get("sensor_count", 0) > 0
            ),
            "autonomous_capabilities": {
                "duty_scheduling": trigger_snapshot.get("resource_count", 0) > 0,
                "goal_health_checks": worker_scheduler is not None,
                "sensing": sensor_snapshot.get("sensor_count", 0) > 0,
            },
            "scheduler": _scheduler_snapshot(worker_scheduler),
            "triggers": trigger_snapshot,
            "sensors": sensor_snapshot,
            "reload_status": _worker_reload_status_snapshot(
                worker_reload_status,
                tenant_id=tenant_id,
                worker_id=worker.worker_id,
            ),
            "runtime": {
                "worker_dir_exists": worker_dir.exists(),
                "duties_count": _count_markdown_files(worker_dir / "duties"),
                "goals_count": _count_markdown_files(worker_dir / "goals"),
                "active_task_count": _count_json_files(worker_dir / "tasks" / "active"),
            },
        })

    return {
        "tenant_id": tenant_id,
        "worker_count": len(workers),
        "workers": workers,
        "persona_reload": persona_reload,
    }


@router.post("/ops/reload")
async def worker_ops_reload(
    request: Request,
    worker_id: str = Query(...),
    tenant_id: str = Query(default="demo"),
) -> dict:
    """Reload one worker from PERSONA.md and refresh heartbeat strategy."""
    require_api_auth(request)
    enforce_worker_scope(request, worker_id)

    reload_worker_runtime = getattr(request.app.state, "reload_worker_runtime", None)
    if reload_worker_runtime is None:
        raise HTTPException(status_code=503, detail="worker reload is not available")
    try:
        result = await reload_worker_runtime(worker_id=worker_id, tenant_id=tenant_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("[worker_routes] Reload failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "tenant_id": tenant_id,
        "status": "reloaded",
        **result,
    }


@router.get("/ops/config")
async def worker_ops_config(
    request: Request,
    worker_id: str = Query(...),
    tenant_id: str = Query(default="demo"),
) -> dict:
    """Return worker-facing config files for local admin UI rendering."""
    require_api_auth(request)

    workspace_root = resolve_workspace_root(getattr(request.app.state, "workspace_root", None))
    worker_dir = workspace_root / "tenants" / tenant_id / "workers" / worker_id
    if not worker_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Worker directory not found: {worker_dir}")

    persona_path = worker_dir / "PERSONA.md"
    if not persona_path.is_file():
        raise HTTPException(status_code=404, detail=f"PERSONA.md not found: {persona_path}")

    return {
        "tenant_id": tenant_id,
        "worker_id": worker_id,
        "worker_dir": str(worker_dir),
        "persona": {
            "filename": persona_path.name,
            "path": str(persona_path),
            "content": persona_path.read_text(encoding="utf-8"),
        },
        "duties": _load_markdown_documents(worker_dir / "duties"),
        "goals": _load_markdown_documents(worker_dir / "goals"),
        "credentials": {
            "filename": "CHANNEL_CREDENTIALS.json",
            "path": str(worker_dir / "CHANNEL_CREDENTIALS.json"),
            "exists": (worker_dir / "CHANNEL_CREDENTIALS.json").is_file(),
        },
    }


def _scheduler_snapshot(worker_scheduler) -> dict:
    """Build a serializable snapshot of worker scheduler state."""
    if worker_scheduler is None:
        return {
            "registered": False,
            "active_count": 0,
            "queue_size": 0,
            "daily_count": 0,
            "max_concurrent_tasks": 0,
            "daily_task_quota": 0,
            "goal_check_enabled": False,
        }

    config = getattr(worker_scheduler, "config", None)
    return {
        "registered": True,
        "active_count": getattr(worker_scheduler, "active_count", 0),
        "queue_size": getattr(worker_scheduler, "queue_size", 0),
        "daily_count": getattr(worker_scheduler, "daily_count", 0),
        "max_concurrent_tasks": getattr(config, "max_concurrent_tasks", 0),
        "daily_task_quota": getattr(config, "daily_task_quota", 0),
        "goal_check_enabled": getattr(config, "goal_check_enabled", False),
    }


def _count_markdown_files(path: Path) -> int:
    """Count markdown files if a directory exists."""
    if not path.is_dir():
        return 0
    return len(tuple(path.glob("*.md")))


def _count_json_files(path: Path) -> int:
    """Count json task files if a directory exists."""
    if not path.is_dir():
        return 0
    return len(tuple(path.glob("*.json")))


def _load_markdown_documents(path: Path) -> list[dict]:
    """Load markdown documents from one worker subdirectory for admin inspection."""
    if not path.is_dir():
        return []

    documents: list[dict] = []
    for item in sorted(path.glob("*.md")):
        documents.append({
            "filename": item.name,
            "path": str(item),
            "content": item.read_text(encoding="utf-8"),
        })
    return documents


def _persona_reload_snapshot(app) -> dict:
    """Build a serializable snapshot for PERSONA auto-reload runtime."""
    watcher = getattr(app.state, "persona_reload_watcher", None)
    if watcher is not None and hasattr(watcher, "operational_snapshot"):
        return watcher.operational_snapshot

    bootstrap_context = getattr(app.state, "bootstrap_context", None)
    settings = getattr(bootstrap_context, "settings", None)
    return {
        "configured": bool(
            getattr(settings, "persona_auto_reload_enabled", False)
        ),
        "running": False,
        "interval_seconds": float(
            getattr(settings, "persona_auto_reload_interval_seconds", 2.0)
        ),
        "debounce_seconds": float(
            getattr(settings, "persona_auto_reload_debounce_seconds", 1.0)
        ),
        "tracked_workers": 0,
        "tracked_files": 0,
        "reload_count": 0,
        "last_scan_completed_at": None,
        "last_error": "",
        "recent_reloads": [],
    }


def _worker_reload_status_snapshot(
    worker_reload_status: dict,
    *,
    tenant_id: str,
    worker_id: str,
) -> dict:
    """Return last reload metadata for one worker if present."""
    return dict(worker_reload_status.get((tenant_id, worker_id), {}))
