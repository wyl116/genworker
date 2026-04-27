"""
Chat conversation streaming routes.

POST /api/v1/chat/stream - SSE streaming endpoint for conversations
GET /api/v1/chat/{thread_id}/tasks - Query spawned task statuses
"""
import asyncio
import time
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.api.auth import enforce_worker_scope, require_api_auth
from src.api.service_ingress import (
    build_service_queued_error,
    merge_session_metadata,
    prepare_service_ingress,
)
from src.common.exceptions import ConfigException
from src.common.logger import get_logger
from src.streaming.event_adapter import create_sse_formatter
from src.streaming.events import ErrorEvent, QueueStatusEvent, StreamEvent

logger = get_logger()

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


class ChatRequest(BaseModel):
    """Request body for POST /api/v1/chat/stream."""

    model_config = {"frozen": True}

    message: str = Field(..., min_length=1, description="User message")
    thread_id: str = Field(..., min_length=1, description="Conversation thread ID")
    tenant_id: str = Field(..., min_length=1, description="Tenant identifier")
    worker_id: Optional[str] = Field(
        default=None, description="Optional worker ID",
    )
    channel_type: Optional[str] = Field(
        default=None, description="Optional inbound channel type",
    )
    channel_id: Optional[str] = Field(
        default=None, description="Optional inbound channel identifier",
    )
    display_name: Optional[str] = Field(
        default=None, description="Optional display name from inbound channel",
    )
    topic: Optional[str] = Field(
        default=None, description="Optional service-topic hint",
    )


class TaskStatusResponse(BaseModel):
    """Response item for task status queries."""
    task_id: str
    status: str
    task_description: str = ""
    result_summary: str = ""
    error_message: str = ""
    created_at: str = ""


class QueueStatusResponse(BaseModel):
    """Queue status for a service-mode chat thread."""

    thread_id: str
    tenant_id: str
    worker_id: str
    status: str
    position: int = 0
    queue_size: int = 0


async def _generate_queue_status_sse(
    request: Request,
    *,
    thread_id: str,
    tenant_id: str,
    worker_id: str,
    timeout_seconds: float,
    poll_interval: float,
    protocol: str,
) -> AsyncGenerator[str, None]:
    """Poll queue status and emit SSE updates until activation or timeout."""
    deadline = time.monotonic() + max(timeout_seconds, 0.1)
    previous: tuple[str, int, int] | None = None
    formatter = create_sse_formatter(protocol=protocol, thread_id=thread_id)

    while time.monotonic() <= deadline:
        if await request.is_disconnected():
            return

        status = await _resolve_queue_status(
            request=request,
            thread_id=thread_id,
            tenant_id=tenant_id,
            worker_id=worker_id,
        )
        current = (status.status, status.position, status.queue_size)
        if current != previous:
            previous = current
            for line in formatter.format(QueueStatusEvent(
                run_id=f"queue-{thread_id}",
                thread_id=thread_id,
                tenant_id=tenant_id,
                worker_id=worker_id,
                status=status.status,
                position=status.position,
                queue_size=status.queue_size,
            )):
                yield line
        # "not_queued" can be a transient state between queue removal and
        # session activation when a retried service request is being admitted.
        if status.status == "active":
            return
        if status.status not in {"queued", "not_queued"}:
            return
        await asyncio.sleep(max(poll_interval, 0.05))

    status = await _resolve_queue_status(
        request=request,
        thread_id=thread_id,
        tenant_id=tenant_id,
        worker_id=worker_id,
    )
    if previous != (status.status, status.position, status.queue_size):
        for line in formatter.format(QueueStatusEvent(
            run_id=f"queue-{thread_id}",
            thread_id=thread_id,
            tenant_id=tenant_id,
            worker_id=worker_id,
            status=status.status,
            position=status.position,
            queue_size=status.queue_size,
        )):
            yield line


async def _generate_chat_sse(
    request: Request,
    message: str,
    thread_id: str,
    tenant_id: str,
    worker_id: Optional[str],
    channel_type: Optional[str] = None,
    channel_id: Optional[str] = None,
    display_name: Optional[str] = None,
    topic: Optional[str] = None,
    protocol: str = "ag-ui",
) -> AsyncGenerator[str, None]:
    """
    Async generator for chat SSE stream.

    1. Get or create ConversationSession via SessionManager
    2. Route message through WorkerRouter
    3. Yield SSE events
    4. Save updated session
    """
    app = request.app
    formatter = create_sse_formatter(protocol=protocol, thread_id=thread_id)

    # Get WorkerRouter from app.state early to derive mode-specific session policy.
    worker_router = getattr(app.state, "worker_router", None)
    if worker_router is None:
        error_event = ErrorEvent(
            run_id="",
            code="WORKER_ROUTER_NOT_AVAILABLE",
            message="Worker router not initialized",
        )
        for line in formatter.format(error_event):
            yield line
        return

    try:
        resolved_entry = worker_router.resolve_entry(
            task=message,
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
        resolved_entry.worker.worker_id if resolved_entry is not None else ""
    )

    # Get SessionManager from app.state (set by ConversationInitializer)
    session_manager = getattr(app.state, "session_manager", None)
    if session_manager is None:
        error_event = ErrorEvent(
            run_id="",
            code="CONVERSATION_NOT_INITIALIZED",
            message="Conversation subsystem not initialized",
        )
        for line in formatter.format(error_event):
            yield line
        return

    session_ttl = None
    session_metadata: dict[str, str] = {}
    service_task_context = ""
    effective_thread_id = thread_id

    if resolved_entry is not None and resolved_entry.worker.is_service:
        service_state = await prepare_service_ingress(
            session_manager=session_manager,
            worker_router=worker_router,
            worker=resolved_entry.worker,
            tenant_id=tenant_id,
            message=message,
            thread_id=thread_id,
            default_channel_type="chat",
            default_channel_id=thread_id,
            channel_type=channel_type,
            channel_id=channel_id,
            display_name=display_name,
            topic=topic,
        )
        if service_state.queued_position is not None:
            for line in formatter.format(
                build_service_queued_error(service_state.queued_position)
            ):
                yield line
            return
        session_ttl = service_state.session_ttl
        session_metadata = service_state.session_metadata
        service_task_context = service_state.task_context
        effective_thread_id = service_state.thread_id
        formatter = create_sse_formatter(
            protocol=protocol,
            thread_id=service_state.thread_id,
        )

    from src.conversation.models import ChatMessage

    # Get or create session
    session = await session_manager.get_or_create(
        thread_id=effective_thread_id,
        tenant_id=tenant_id,
        worker_id=resolved_worker_id,
        ttl_seconds=session_ttl,
        metadata=session_metadata,
    )
    if service_task_context:
        from dataclasses import replace

        session = replace(
            session,
            metadata=merge_session_metadata(
                session.metadata, {"service_profile_context": service_task_context},
            ),
        )

    # Append user message
    user_msg = ChatMessage(role="user", content=message)
    session = session.append_message(user_msg)
    assistant_parts: list[str] = []
    spawned_task_ids: list[str] = []

    try:
        async for event in worker_router.route_stream(
            task=message,
            tenant_id=tenant_id,
            worker_id=resolved_worker_id or worker_id,
            task_context=service_task_context,
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
    except Exception as exc:
        logger.error(f"[chat_routes] Stream error: {exc}", exc_info=True)
        error_event = ErrorEvent(
            run_id="",
            code="CHAT_STREAM_ERROR",
            message=str(exc),
        )
        for line in formatter.format(error_event):
            yield line

    if assistant_parts:
        session = session.append_message(ChatMessage(
            role="assistant",
            content="\n\n".join(
                part.strip() for part in assistant_parts if str(part).strip()
            ),
        ))
    for task_id in spawned_task_ids:
        session = session.add_spawned_task(task_id)

    # Save session after interaction
    await session_manager.save(session)


@router.post("/stream")
async def chat_stream(
    request_body: ChatRequest,
    request: Request,
    protocol: str = Query(default="ag-ui", pattern="^(ag-ui|legacy)$"),
) -> StreamingResponse:
    """
    Stream a conversation interaction as Server-Sent Events.

    Flow:
    1. SessionManager gets/creates ConversationSession
    2. Skill matching per message
    3. Engine execution via WorkerRouter
    4. SSE streaming response
    5. Session state updated
    """
    require_api_auth(request)
    if request_body.worker_id:
        enforce_worker_scope(request, request_body.worker_id)

    logger.info(
        f"[chat_routes] Chat stream: tenant={request_body.tenant_id}, "
        f"thread={request_body.thread_id}, "
        f"message={request_body.message[:80]}"
    )

    return StreamingResponse(
        _generate_chat_sse(
            request=request,
            message=request_body.message,
            thread_id=request_body.thread_id,
            tenant_id=request_body.tenant_id,
            worker_id=request_body.worker_id,
            channel_type=request_body.channel_type,
            channel_id=request_body.channel_id,
            display_name=request_body.display_name,
            topic=request_body.topic,
            protocol=protocol,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{thread_id}/tasks")
async def list_chat_tasks(
    thread_id: str,
    request: Request,
) -> list[TaskStatusResponse]:
    """
    Query all async tasks spawned from a conversation thread.

    Looks up the ConversationSession by thread_id and returns
    the status of each spawned task.
    """
    require_api_auth(request)

    session_manager = getattr(request.app.state, "session_manager", None)
    task_store = getattr(request.app.state, "task_store", None)

    if session_manager is None or task_store is None:
        return []

    session = await session_manager.get_or_create(
        thread_id=thread_id,
        tenant_id="",
        worker_id="",
    )

    results: list[TaskStatusResponse] = []
    for task_id in session.spawned_tasks:
        # Search for the task across all workers for this tenant
        manifest = _find_task_manifest(
            task_store, session.tenant_id, task_id,
        )
        if manifest is not None:
            results.append(
                TaskStatusResponse(
                    task_id=manifest.task_id,
                    status=manifest.status.value,
                    task_description=manifest.task_description,
                    result_summary=manifest.result_summary,
                    error_message=manifest.error_message,
                    created_at=manifest.created_at,
                )
            )

    return results


def _find_task_manifest(task_store, tenant_id: str, task_id: str):
    """Find a task manifest by scanning worker directories."""
    from pathlib import Path

    base_dir = task_store._workspace_root / "tenants" / tenant_id / "workers"
    if not base_dir.is_dir():
        return None

    for worker_dir in base_dir.iterdir():
        if worker_dir.is_dir():
            manifest = task_store.load(
                tenant_id, worker_dir.name, task_id,
            )
            if manifest is not None:
                return manifest
    return None


@router.get("/{thread_id}/queue")
async def get_chat_queue_status(
    thread_id: str,
    request: Request,
    tenant_id: str = Query(...),
    worker_id: str = Query(...),
) -> QueueStatusResponse:
    """Query the queue/active status for a service chat thread."""
    require_api_auth(request)
    enforce_worker_scope(request, worker_id)
    return await _resolve_queue_status(
        request=request,
        thread_id=thread_id,
        tenant_id=tenant_id,
        worker_id=worker_id,
    )


@router.get("/{thread_id}/queue/stream")
async def stream_chat_queue_status(
    thread_id: str,
    request: Request,
    tenant_id: str = Query(...),
    worker_id: str = Query(...),
    timeout_seconds: float = Query(default=5.0, ge=0.1, le=30.0),
    poll_interval: float = Query(default=0.2, ge=0.05, le=5.0),
    protocol: str = Query(default="ag-ui", pattern="^(ag-ui|legacy)$"),
) -> StreamingResponse:
    """Stream queue status transitions for a service chat thread."""
    require_api_auth(request)
    enforce_worker_scope(request, worker_id)
    return StreamingResponse(
        _generate_queue_status_sse(
            request=request,
            thread_id=thread_id,
            tenant_id=tenant_id,
            worker_id=worker_id,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            protocol=protocol,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _resolve_queue_status(
    *,
    request: Request,
    thread_id: str,
    tenant_id: str,
    worker_id: str,
) -> QueueStatusResponse:
    """Resolve current queue or activity state for a thread."""
    session_manager = getattr(request.app.state, "session_manager", None)
    if session_manager is None:
        return QueueStatusResponse(
            thread_id=thread_id,
            tenant_id=tenant_id,
            worker_id=worker_id,
            status="unavailable",
        )

    session = await session_manager.find_by_thread(thread_id)
    if (
        session is not None
        and session.tenant_id == tenant_id
        and session.worker_id == worker_id
        and session_manager.is_session_active(session)
    ):
        return QueueStatusResponse(
            thread_id=thread_id,
            tenant_id=tenant_id,
            worker_id=worker_id,
            status="active",
        )

    position = session_manager.get_service_queue_position(
        tenant_id=tenant_id,
        worker_id=worker_id,
        thread_id=thread_id,
    )
    if position is not None:
        return QueueStatusResponse(
            thread_id=thread_id,
            tenant_id=tenant_id,
            worker_id=worker_id,
            status="queued",
            position=position,
            queue_size=session_manager.get_service_queue_size(
                tenant_id=tenant_id,
                worker_id=worker_id,
            ),
        )

    return QueueStatusResponse(
        thread_id=thread_id,
        tenant_id=tenant_id,
        worker_id=worker_id,
        status="not_queued",
    )
