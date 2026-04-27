"""
Shared helpers for service-mode API ingress handling.
"""
from dataclasses import dataclass
import hashlib
from typing import Mapping

from src.streaming.events import ErrorEvent


@dataclass(frozen=True)
class ServiceIngressState:
    """Prepared service-mode state for one inbound request."""

    thread_id: str
    session_ttl: int | None
    session_metadata: dict[str, str]
    task_context: str
    queued_position: int | None = None


async def prepare_service_ingress(
    *,
    session_manager,
    worker_router,
    worker,
    tenant_id: str,
    message: str,
    thread_id: str | None,
    default_channel_type: str,
    default_channel_id: str,
    channel_type: str | None = None,
    channel_id: str | None = None,
    display_name: str | None = None,
    topic: str | None = None,
    metadata: Mapping[str, str] | None = None,
) -> ServiceIngressState:
    """Prepare session policy and customer context for a service-mode request."""
    metadata = metadata or {}
    effective_channel_type = channel_type or metadata.get("channel_type") or default_channel_type
    effective_channel_id = channel_id or metadata.get("channel_id") or default_channel_id
    effective_display_name = display_name or metadata.get("display_name") or ""
    effective_topic = topic or metadata.get("topic") or ""
    effective_thread_id = resolve_service_thread_id(
        tenant_id=tenant_id,
        worker_id=worker.worker_id,
        task=message,
        thread_id=thread_id,
        channel_type=effective_channel_type,
        channel_id=effective_channel_id,
    )
    service_config = worker.service_config
    session_ttl = service_config.session_ttl if service_config is not None else 0

    existing_session = await session_manager.find_by_thread(effective_thread_id)
    if existing_session is None and service_config is not None:
        active_sessions = await session_manager.count_active_sessions(
            tenant_id=tenant_id,
            worker_id=worker.worker_id,
            exclude_thread_id=effective_thread_id,
            ttl_seconds=session_ttl,
        )
        if (
            service_config.max_concurrent_sessions > 0
            and active_sessions >= service_config.max_concurrent_sessions
        ):
            queue_position = session_manager.enqueue_service_session(
                tenant_id=tenant_id,
                worker_id=worker.worker_id,
                thread_id=effective_thread_id,
                ttl_seconds=min(service_config.session_ttl or 300, 300),
            )
            return ServiceIngressState(
                thread_id=effective_thread_id,
                session_ttl=session_ttl,
                session_metadata={
                    "channel_type": effective_channel_type,
                    "channel_id": effective_channel_id or effective_thread_id,
                    **({"display_name": effective_display_name} if effective_display_name else {}),
                },
                task_context="",
                queued_position=queue_position,
            )

    session_manager.dequeue_service_session(
        tenant_id=tenant_id,
        worker_id=worker.worker_id,
        thread_id=effective_thread_id,
    )
    task_context = await build_service_task_context(
        worker_router=worker_router,
        worker_id=worker.worker_id,
        channel_type=effective_channel_type,
        channel_id=effective_channel_id or effective_thread_id,
        message=message,
        display_name=effective_display_name,
        topic=effective_topic,
    )
    session_metadata = {
        "channel_type": effective_channel_type,
        "channel_id": effective_channel_id or effective_thread_id,
        **({"display_name": effective_display_name} if effective_display_name else {}),
        **({"service_profile_context": task_context} if task_context else {}),
    }
    return ServiceIngressState(
        thread_id=effective_thread_id,
        session_ttl=session_ttl,
        session_metadata=session_metadata,
        task_context=task_context,
    )


def build_service_queued_error(queue_position: int) -> ErrorEvent:
    """Build the standard queued response for busy service workers."""
    return ErrorEvent(
        run_id="",
        code="SERVICE_QUEUED",
        message=(
            "Service is busy right now. "
            f"Your request is queued at position {queue_position}. "
            "Please retry in a moment."
        ),
    )


async def build_service_task_context(
    *,
    worker_router,
    worker_id: str,
    channel_type: str,
    channel_id: str,
    message: str,
    display_name: str,
    topic: str,
) -> str:
    """Resolve and summarize the current service customer's profile."""
    registry = worker_router.get_contact_registry(worker_id)
    if registry is None:
        return ""

    from src.worker.contacts.discovery import PersonExtractor

    extractor = PersonExtractor(contact_registry=registry, llm_client=None)
    profile = await extractor.extract_service_profile(
        channel_type=channel_type,
        channel_id=channel_id,
        message=message,
        display_name=display_name,
        topic=topic,
    )
    return format_service_profile_context(profile)


def format_service_profile_context(profile) -> str:
    """Format a lightweight service-customer profile for task context."""
    if profile is None:
        return ""

    channels = ", ".join(
        f"{identity.channel_type}:{identity.handle}"
        for identity in profile.identities
        if identity.channel_type and identity.handle
    )
    tags = ", ".join(profile.tags)
    topics = ", ".join(profile.common_topics)
    name = profile.primary_name or "anonymous customer"

    lines = [
        "[Service Customer]",
        f"Name: {name}",
        f"Service count: {profile.service_count}",
    ]
    if channels:
        lines.append(f"Channels: {channels}")
    if tags:
        lines.append(f"Known identifiers: {tags}")
    if topics:
        lines.append(f"Common topics: {topics}")
    if profile.notes:
        lines.append(f"Latest note: {profile.notes}")
    return "\n".join(lines)


def merge_session_metadata(
    existing: tuple[tuple[str, str], ...],
    incoming: dict[str, str],
) -> tuple[tuple[str, str], ...]:
    """Merge stored session metadata with incoming values."""
    merged = dict(existing)
    merged.update({str(k): str(v) for k, v in incoming.items()})
    return tuple(merged.items())


def resolve_service_thread_id(
    *,
    tenant_id: str,
    worker_id: str,
    task: str,
    thread_id: str | None,
    channel_type: str,
    channel_id: str,
) -> str:
    """Derive a stable thread ID for service-mode requests."""
    if thread_id:
        return thread_id
    if channel_type and channel_id:
        return f"service:{channel_type}:{channel_id}"
    digest = hashlib.sha1(
        f"{tenant_id}:{worker_id}:{task}".encode("utf-8")
    ).hexdigest()[:16]
    return f"service-task:{digest}"
