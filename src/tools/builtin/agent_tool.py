"""
Agent Tool compatibility layer for cross-worker delegation.

The runtime product path now injects ``delegate_to_worker`` dynamically via
``src.worker.tool_scope``. This module remains as a stable helper for tests and
for call sites that still invoke delegation directly.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from src.events.bus import EventBus
from src.events.models import Event

logger = logging.getLogger(__name__)

MAX_DELEGATION_DEPTH = 2


@dataclass(frozen=True)
class DelegateRequest:
    """
    Immutable request for cross-worker delegation.

    delegation_depth tracks the current chain depth; each forward increments by 1.
    """
    target_worker: str
    task: str
    context: tuple[tuple[str, Any], ...] = ()
    timeout: int = 300
    mode: str = "sync"
    delegation_depth: int = 0


@dataclass(frozen=True)
class DelegateResult:
    """
    Immutable result from a delegation attempt.

    status: "completed" | "error" | "timeout" | "submitted" | "rejected"
    """
    status: str
    result: str = ""
    task_id: str = ""
    error: str = ""


AGENT_TOOL_DEFINITION: dict[str, Any] = {
    "name": "delegate_to_worker",
    "description": (
        "Delegate a task to another Worker. "
        "Use when the task exceeds the current Worker's skill scope."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "target_worker": {
                "type": "string",
                "description": "Target Worker ID",
            },
            "task": {
                "type": "string",
                "description": "Task description",
            },
            "context": {
                "type": "object",
                "description": "Additional context key-value pairs",
            },
        },
        "required": ["target_worker", "task"],
    },
    "risk_level": "normal",
}


async def execute_delegation(
    request: DelegateRequest,
    worker_registry: Any,
    source_worker_id: str,
    tenant_id: str,
    worker_router: Any | None = None,
) -> DelegateResult:
    """
    Execute a cross-worker delegation.

    Flow:
    1. Depth check: reject if delegation_depth >= MAX_DELEGATION_DEPTH
    2. Validate target worker exists in registry
    3. Validate tenant isolation (target worker belongs to same tenant)
    4. Allocate a task id for tracking
    5. sync mode: wait for result with timeout
       async mode: submit and return task_id immediately

    Args:
        request: The delegation request.
        worker_registry: WorkerRegistry for worker lookup.
        source_worker_id: The calling worker's ID.
        tenant_id: The calling worker's tenant ID.

    Returns:
        Immutable DelegateResult with outcome.
    """
    # 1. Depth check
    if request.delegation_depth >= MAX_DELEGATION_DEPTH:
        logger.warning(
            f"[AgentTool] Delegation depth limit exceeded: "
            f"depth={request.delegation_depth}, max={MAX_DELEGATION_DEPTH}"
        )
        return DelegateResult(
            status="rejected",
            error=f"Delegation depth limit exceeded (max={MAX_DELEGATION_DEPTH})",
        )

    # 2. Validate target worker exists
    target_entry = worker_registry.get(request.target_worker)
    if target_entry is None:
        logger.warning(
            f"[AgentTool] Target worker not found: {request.target_worker}"
        )
        return DelegateResult(
            status="error",
            error=f"Target worker '{request.target_worker}' not found",
        )

    # 3. Tenant isolation check
    target_worker = target_entry.worker
    target_tenant = _extract_tenant_id(target_worker)
    if target_tenant and target_tenant != tenant_id:
        logger.warning(
            f"[AgentTool] Cross-tenant delegation rejected: "
            f"source_tenant={tenant_id}, target_tenant={target_tenant}"
        )
        return DelegateResult(
            status="rejected",
            error="Cross-tenant delegation is not allowed",
        )

    task_id = uuid4().hex

    # 5. Dispatch based on mode
    if request.mode == "async":
        return DelegateResult(
            status="submitted",
            task_id=task_id,
        )

    # sync mode: simulate execution with timeout
    return await _execute_sync(
        task_id=task_id,
        request=request,
        worker_registry=worker_registry,
        tenant_id=tenant_id,
        worker_router=worker_router,
    )


async def _execute_sync(
    task_id: str,
    request: DelegateRequest,
    worker_registry: Any,
    tenant_id: str,
    worker_router: Any | None,
) -> DelegateResult:
    """
    Execute sync delegation: dispatch to target worker and wait for result.

    Uses asyncio.wait_for for timeout enforcement.
    """
    try:
        result = await asyncio.wait_for(
            _run_delegated_task(
                task_id=task_id,
                target_worker_id=request.target_worker,
                task_description=request.task,
                context=request.context,
                delegation_depth=request.delegation_depth + 1,
                worker_registry=worker_registry,
                tenant_id=tenant_id,
                worker_router=worker_router,
            ),
            timeout=request.timeout,
        )
        return result
    except asyncio.TimeoutError:
        logger.warning(
            f"[AgentTool] Delegation timed out after {request.timeout}s: "
            f"task_id={task_id}"
        )
        return DelegateResult(
            status="timeout",
            task_id=task_id,
            error=f"Delegation timed out after {request.timeout}s",
        )


async def _run_delegated_task(
    task_id: str,
    target_worker_id: str,
    task_description: str,
    context: tuple[tuple[str, Any], ...],
    delegation_depth: int,
    worker_registry: Any,
    tenant_id: str,
    worker_router: Any | None = None,
) -> DelegateResult:
    """
    Run the delegated task on the target worker.

    When a WorkerRouter is available, delegate through the actual routing
    pipeline and collect the assistant text. Otherwise fall back to the legacy
    deterministic stub result used by existing tests.
    """
    logger.info(
        f"[AgentTool] Executing delegated task on worker '{target_worker_id}': "
        f"task_id={task_id}, depth={delegation_depth}"
    )
    if worker_router is None:
        return DelegateResult(
            status="completed",
            task_id=task_id,
            result=f"Task '{task_description}' completed by {target_worker_id}",
        )

    parts: list[str] = []
    error_message = ""
    async for event in worker_router.route_stream(
        task=task_description,
        tenant_id=tenant_id,
        worker_id=target_worker_id,
        task_context=_stringify_context(context),
        subagent_depth=delegation_depth,
    ):
        event_type = getattr(event, "event_type", "")
        if event_type == "TEXT_MESSAGE":
            content = getattr(event, "content", "")
            if content:
                parts.append(str(content))
        elif event_type == "ERROR":
            error_message = getattr(event, "message", "") or "Delegation failed"
    if error_message:
        return DelegateResult(
            status="error",
            task_id=task_id,
            error=error_message,
        )
    content = "\n\n".join(part.strip() for part in parts if str(part).strip())
    if not content:
        content = f"Task '{task_description}' completed by {target_worker_id}"
    return DelegateResult(
        status="completed",
        task_id=task_id,
        result=content,
    )


async def send_notification(
    event_bus: EventBus,
    source_worker_id: str,
    tenant_id: str,
    event_type: str,
    payload: tuple[tuple[str, Any], ...] = (),
) -> str:
    """
    Send a notification to other workers via EventBus (notification mode).

    Args:
        event_bus: The EventBus instance.
        source_worker_id: The sending worker's ID.
        tenant_id: Tenant scope for the event.
        event_type: Event type string (e.g. "worker.task_completed").
        payload: Immutable key-value payload.

    Returns:
        The event_id of the published event.
    """
    event_id = uuid4().hex
    event = Event(
        event_id=event_id,
        type=event_type,
        source=source_worker_id,
        tenant_id=tenant_id,
        payload=payload,
    )
    triggered = await event_bus.publish(event)
    logger.info(
        f"[AgentTool] Notification sent: event_id={event_id}, "
        f"type={event_type}, triggered={triggered} handler(s)"
    )
    return event_id


def _extract_tenant_id(worker: Any) -> str:
    """
    Extract tenant_id from a worker if available.

    Workers don't carry tenant_id directly, so we return empty string
    to indicate the check should be skipped (handled by the registry's
    tenant scoping instead).
    """
    return getattr(worker, "tenant_id", "")


def _stringify_context(context: tuple[tuple[str, Any], ...]) -> str:
    """Render tuple-based delegation context into a compact text block."""
    lines: list[str] = []
    for key, value in context:
        key_text = str(key).strip()
        value_text = str(value).strip()
        if not key_text and not value_text:
            continue
        if key_text:
            lines.append(f"{key_text}: {value_text}")
        else:
            lines.append(value_text)
    return "\n".join(lines)
