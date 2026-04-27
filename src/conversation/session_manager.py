"""
SessionManager - conversation session lifecycle management.

Responsibilities:
- Get or create sessions by thread_id
- Save updated sessions after each interaction
- Clean up expired sessions based on TTL
- Track failed tasks via EventBus subscription
"""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from src.common.logger import get_logger

from .models import ChatMessage, ConversationSession, _now_iso
from .session_store import SessionStore

logger = get_logger()

# Default TTL: 1 hour
DEFAULT_TTL_SECONDS = 3600
DEFAULT_SERVICE_QUEUE_TTL_SECONDS = 300


@dataclass
class _QueuedServiceRequest:
    """Lightweight queued service session marker."""

    thread_id: str
    queued_at: str
    ttl_seconds: int = DEFAULT_SERVICE_QUEUE_TTL_SECONDS


class SessionManager:
    """
    Conversation session lifecycle manager.

    Manages creation, retrieval, persistence, and expiration
    of ConversationSession objects.
    """

    def __init__(
        self,
        store: SessionStore,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        search_index=None,
    ) -> None:
        self._store = store
        self._ttl_seconds = ttl_seconds
        self._search_index = search_index
        # In-memory cache: thread_id -> session
        self._cache: dict[str, ConversationSession] = {}
        # Failed task notifications: session_id -> list of error messages
        self._failed_tasks: dict[str, list[str]] = {}
        # Service wait queue: (tenant_id, worker_id) -> queued thread_ids
        self._service_wait_queues: dict[tuple[str, str], list[_QueuedServiceRequest]] = {}
        self._memory_orchestrator = None

    def set_memory_orchestrator(self, orchestrator) -> None:
        """Inject the optional memory orchestrator after construction."""
        self._memory_orchestrator = orchestrator

    async def get_or_create(
        self,
        thread_id: str,
        tenant_id: str,
        worker_id: str,
        ttl_seconds: int | None = None,
        metadata: dict[str, str] | None = None,
        session_type: str = "thread",
        main_session_key: str | None = None,
    ) -> ConversationSession:
        """
        Get an existing session for thread_id or create a new one.

        If there are pending failed task notifications, they are
        appended as system messages to the session.
        """
        # Check in-memory cache first
        session = self._cache.get(thread_id)

        # Try persistent store
        if session is None:
            session = await self._store.find_by_thread(thread_id)

        # Create new if not found
        if session is None:
            session = ConversationSession(
                session_id=uuid4().hex,
                thread_id=thread_id,
                tenant_id=tenant_id,
                worker_id=worker_id,
                ttl_seconds=ttl_seconds or 0,
                metadata=_metadata_to_tuple(metadata),
                session_type=session_type,
                main_session_key=main_session_key,
            )
            logger.info(
                f"[SessionManager] Created new session "
                f"{session.session_id} for thread {thread_id}"
            )
        else:
            updated_metadata = _merge_metadata(session.metadata, metadata)
            next_ttl = ttl_seconds if ttl_seconds is not None else session.ttl_seconds
            next_session_type = session_type or session.session_type
            next_main_session_key = (
                main_session_key
                if main_session_key is not None
                else session.main_session_key
            )
            if (
                updated_metadata != session.metadata
                or next_ttl != session.ttl_seconds
                or next_session_type != session.session_type
                or next_main_session_key != session.main_session_key
            ):
                session = replace(
                    session,
                    ttl_seconds=next_ttl,
                    metadata=updated_metadata,
                    session_type=next_session_type,
                    main_session_key=next_main_session_key,
                )

        # Inject failed task notifications as system messages
        session = self._inject_failed_task_messages(session)

        # Update cache
        self._cache[thread_id] = session
        return session

    async def save(self, session: ConversationSession) -> None:
        """Save session to store and update cache."""
        await self._store.save(session)
        self._cache[session.thread_id] = session
        if self._search_index is not None:
            try:
                await self._search_index.upsert_session_messages(session)
            except Exception as exc:
                logger.warning("[SessionManager] Session search upsert failed: %s", exc)

    async def find_by_thread(self, thread_id: str) -> ConversationSession | None:
        """Find a session by thread ID from cache or store."""
        cached = self._cache.get(thread_id)
        if cached is not None:
            return cached
        session = await self._store.find_by_thread(thread_id)
        if session is not None:
            self._cache[thread_id] = session
        return session

    async def count_active_sessions(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        exclude_thread_id: str = "",
        ttl_seconds: int | None = None,
    ) -> int:
        """Count non-expired sessions for one tenant/worker pair."""
        now = datetime.now(timezone.utc)
        persisted = await self._store.list_sessions(tenant_id, worker_id)
        sessions_by_id: dict[str, ConversationSession] = {
            session.session_id: session for session in persisted
        }
        for session in self._cache.values():
            if session.tenant_id != tenant_id or session.worker_id != worker_id:
                continue
            sessions_by_id[session.session_id] = session

        active = 0
        for session in sessions_by_id.values():
            if session.session_type != "thread":
                continue
            if exclude_thread_id and session.thread_id == exclude_thread_id:
                continue
            effective_ttl = _effective_ttl(session, ttl_seconds, self._ttl_seconds)
            if effective_ttl is None:
                continue
            if not _is_expired(session.last_active_at, now, effective_ttl):
                active += 1
        return active

    async def cleanup_expired(self) -> int:
        """
        Remove sessions that have exceeded the TTL.

        Returns the number of sessions cleaned up.
        """
        now = datetime.now(timezone.utc)
        expired_thread_ids: list[str] = []

        for thread_id, session in list(self._cache.items()):
            effective_ttl = _effective_ttl(session, None, self._ttl_seconds)
            if effective_ttl is None:
                continue
            if _is_expired(session.last_active_at, now, effective_ttl):
                expired_thread_ids.append(thread_id)

        cleaned = 0
        for thread_id in expired_thread_ids:
            session = self._cache.pop(thread_id, None)
            if session is not None:
                if self._memory_orchestrator is not None:
                    try:
                        await self._memory_orchestrator.on_session_end(
                            tuple({
                                "role": message.role,
                                "content": message.content,
                                "timestamp": message.timestamp,
                            } for message in session.messages)
                        )
                    except Exception as exc:
                        logger.warning(
                            "[SessionManager] on_session_end failed for %s: %s",
                            thread_id,
                            exc,
                        )
                await self._store.delete(session.session_id)
                if self._search_index is not None:
                    try:
                        await self._search_index.delete_session(session.session_id)
                    except Exception as exc:
                        logger.warning(
                            "[SessionManager] Session search delete failed for %s: %s",
                            session.session_id,
                            exc,
                        )
                cleaned += 1
                logger.info(
                    f"[SessionManager] Cleaned expired session "
                    f"{session.session_id} (thread={thread_id})"
                )

        return cleaned

    def enqueue_service_session(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        thread_id: str,
        ttl_seconds: int | None = None,
    ) -> int:
        """Add a service thread to the wait queue and return its position."""
        key = (tenant_id, worker_id)
        self._cleanup_service_queue(key)
        queue = self._service_wait_queues.setdefault(key, [])
        for index, entry in enumerate(queue, 1):
            if entry.thread_id == thread_id:
                return index
        queue.append(_QueuedServiceRequest(
            thread_id=thread_id,
            queued_at=_now_iso(),
            ttl_seconds=max(ttl_seconds or DEFAULT_SERVICE_QUEUE_TTL_SECONDS, 1),
        ))
        return len(queue)

    def dequeue_service_session(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        thread_id: str,
    ) -> None:
        """Remove a thread from the service wait queue if present."""
        key = (tenant_id, worker_id)
        self._cleanup_service_queue(key)
        queue = self._service_wait_queues.get(key, [])
        filtered = [entry for entry in queue if entry.thread_id != thread_id]
        if filtered:
            self._service_wait_queues[key] = filtered
        else:
            self._service_wait_queues.pop(key, None)

    async def reset_thread(self, thread_id: str) -> None:
        """Delete the current session for a thread and clear cache/index state."""
        session = await self.find_by_thread(thread_id)
        if session is None:
            self._cache.pop(thread_id, None)
            return
        self._cache.pop(thread_id, None)
        await self._store.delete(session.session_id)
        if self._search_index is not None:
            try:
                await self._search_index.delete_session(session.session_id)
            except Exception as exc:
                logger.warning(
                    "[SessionManager] Session search delete failed during reset for %s: %s",
                    session.session_id,
                    exc,
                )

    def get_service_queue_position(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        thread_id: str,
    ) -> int | None:
        """Return the current queue position for a thread if queued."""
        key = (tenant_id, worker_id)
        self._cleanup_service_queue(key)
        queue = self._service_wait_queues.get(key, [])
        for index, entry in enumerate(queue, 1):
            if entry.thread_id == thread_id:
                return index
        return None

    def get_service_queue_size(
        self,
        *,
        tenant_id: str,
        worker_id: str,
    ) -> int:
        """Return current queue size after pruning stale entries."""
        key = (tenant_id, worker_id)
        self._cleanup_service_queue(key)
        return len(self._service_wait_queues.get(key, []))

    def cleanup_stale_service_queues(self) -> int:
        """Prune stale queued service requests across all workers."""
        removed = 0
        for key in list(self._service_wait_queues.keys()):
            removed += self._cleanup_service_queue(key)
        return removed

    def is_session_active(
        self,
        session: ConversationSession,
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        """Check whether a session is still active under its effective TTL."""
        effective_ttl = (
            _effective_ttl(session, ttl_seconds, self._ttl_seconds)
        )
        if effective_ttl is None:
            return True
        return not _is_expired(
            session.last_active_at,
            datetime.now(timezone.utc),
            effective_ttl,
        )

    def record_task_failure(
        self,
        session_id: str,
        error_message: str,
    ) -> None:
        """
        Record a task failure for later injection into the session.

        Called by EventBus task.failed handler.
        """
        failures = self._failed_tasks.setdefault(session_id, [])
        failures.append(error_message)

    def _cleanup_service_queue(
        self,
        key: tuple[str, str],
    ) -> int:
        """Remove stale entries from one service wait queue."""
        queue = self._service_wait_queues.get(key, [])
        if not queue:
            return 0

        now = datetime.now(timezone.utc)
        filtered = [
            entry for entry in queue
            if not _is_expired(entry.queued_at, now, entry.ttl_seconds)
        ]
        removed = len(queue) - len(filtered)
        if filtered:
            self._service_wait_queues[key] = filtered
        else:
            self._service_wait_queues.pop(key, None)
        return removed

    def _inject_failed_task_messages(
        self,
        session: ConversationSession,
    ) -> ConversationSession:
        """Append system messages for any failed tasks."""
        failures = self._failed_tasks.pop(session.session_id, [])
        for failure_msg in failures:
            msg = ChatMessage(
                role="system",
                content=f"Task failed: {failure_msg}",
                timestamp=_now_iso(),
            )
            session = session.append_message(msg)
        return session

    @property
    def cached_session_count(self) -> int:
        """Number of sessions in the in-memory cache."""
        return len(self._cache)


def _is_expired(
    last_active_at: str,
    now: datetime,
    ttl_seconds: int,
) -> bool:
    """Check if a session has exceeded its TTL."""
    if not last_active_at:
        return True
    try:
        last_active = datetime.fromisoformat(last_active_at)
        # Ensure timezone-aware comparison
        if last_active.tzinfo is None:
            last_active = last_active.replace(tzinfo=timezone.utc)
        elapsed = (now - last_active).total_seconds()
        return elapsed > ttl_seconds
    except (ValueError, TypeError):
        return True


def _effective_ttl(
    session: ConversationSession,
    ttl_override: int | None,
    default_ttl: int,
) -> int | None:
    """Return None for long-lived sessions that should never expire."""
    if session.session_type == "main":
        return None
    if session.ttl_seconds > 0:
        return session.ttl_seconds
    if ttl_override is not None:
        return ttl_override
    return default_ttl


def _metadata_to_tuple(metadata: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    """Normalize metadata dict into immutable tuple form."""
    if not metadata:
        return ()
    return tuple((str(k), str(v)) for k, v in metadata.items())


def _merge_metadata(
    existing: tuple[tuple[str, str], ...],
    incoming: dict[str, str] | None,
) -> tuple[tuple[str, str], ...]:
    """Merge new metadata into existing immutable metadata."""
    if not incoming:
        return existing
    merged = dict(existing)
    for key, value in incoming.items():
        merged[str(key)] = str(value)
    return tuple(merged.items())
