"""
Session persistence layer.

Defines:
- SessionStore Protocol for pluggable backends
- FileSessionStore for file-system-based persistence
- RedisSessionStore and HybridSessionStore for runtime acceleration
"""
import json
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from src.common.logger import get_logger

from .models import ConversationSession

logger = get_logger()


@runtime_checkable
class SessionStore(Protocol):
    """Protocol for session persistence backends."""

    async def load(self, session_id: str) -> Optional[ConversationSession]:
        """Load a session by ID."""
        ...

    async def save(self, session: ConversationSession) -> None:
        """Persist a session."""
        ...

    async def delete(self, session_id: str) -> None:
        """Delete a session by ID."""
        ...

    async def find_by_thread(
        self, thread_id: str,
    ) -> Optional[ConversationSession]:
        """Find a session by thread_id."""
        ...

    async def list_sessions(
        self,
        tenant_id: str,
        worker_id: str,
    ) -> tuple[ConversationSession, ...]:
        """List persisted sessions for a tenant/worker pair."""
        ...


class FileSessionStore:
    """
    File-system session store.

    Storage path: workspace/tenants/{tid}/sessions/{session_id}.json
    """

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root

    async def load(self, session_id: str) -> Optional[ConversationSession]:
        """Load a session by scanning tenant directories."""
        for path in self._workspace_root.rglob(f"sessions/{session_id}.json"):
            return self._read_file(path)
        return None

    async def save(self, session: ConversationSession) -> None:
        """Persist a session to the filesystem."""
        sessions_dir = self._sessions_dir(session.tenant_id)
        sessions_dir.mkdir(parents=True, exist_ok=True)

        file_path = sessions_dir / f"{session.session_id}.json"
        data = json.dumps(
            session.to_dict(), ensure_ascii=False, indent=2,
        )
        try:
            file_path.write_text(data, encoding="utf-8")
        except OSError as exc:
            logger.error(
                f"[FileSessionStore] Failed to save session "
                f"{session.session_id}: {exc}"
            )
            raise

    async def delete(self, session_id: str) -> None:
        """Delete a session file."""
        for path in self._workspace_root.rglob(f"sessions/{session_id}.json"):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.error(
                    f"[FileSessionStore] Failed to delete "
                    f"session {session_id}: {exc}"
                )

    async def find_by_thread(
        self, thread_id: str,
    ) -> Optional[ConversationSession]:
        """Find the most recent session for a thread_id."""
        candidates: list[ConversationSession] = []
        sessions_pattern = "tenants/*/sessions/*.json"
        for path in self._workspace_root.glob(sessions_pattern):
            session = self._read_file(path)
            if session is not None and session.thread_id == thread_id:
                candidates.append(session)

        if not candidates:
            return None

        # Return the most recently active session
        return max(candidates, key=lambda s: s.last_active_at)

    async def list_sessions(
        self,
        tenant_id: str,
        worker_id: str,
    ) -> tuple[ConversationSession, ...]:
        """List persisted sessions for one tenant/worker pair."""
        sessions_dir = self._sessions_dir(tenant_id)
        if not sessions_dir.is_dir():
            return ()

        sessions: list[ConversationSession] = []
        for path in sessions_dir.glob("*.json"):
            session = self._read_file(path)
            if session is None:
                continue
            if session.worker_id != worker_id:
                continue
            sessions.append(session)
        return tuple(sessions)

    def _sessions_dir(self, tenant_id: str) -> Path:
        """Resolve sessions directory for a tenant."""
        return self._workspace_root / "tenants" / tenant_id / "sessions"

    def _read_file(self, path: Path) -> Optional[ConversationSession]:
        """Read and parse a session JSON file."""
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return ConversationSession.from_dict(data)
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            logger.error(
                f"[FileSessionStore] Failed to read {path}: {exc}"
            )
            return None


class RedisSessionStore:
    """Redis-backed session persistence with indexed lookups."""

    _PREFIX = "lw:session"
    _THREAD_INDEX = "lw:session_idx:thread"
    _LIST_INDEX = "lw:session_idx:list"

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    async def load(self, session_id: str) -> Optional[ConversationSession]:
        raw = await self._redis.get(self._session_key(session_id))
        return self._deserialize(raw, context=f"session {session_id}")

    async def save(self, session: ConversationSession) -> None:
        payload = json.dumps(session.to_dict(), ensure_ascii=False)
        ttl = session.ttl_seconds if session.session_type == "thread" and session.ttl_seconds > 0 else None
        await self._redis.set(self._session_key(session.session_id), payload, ttl=ttl)
        await self._redis.set(
            self._thread_key(session.thread_id),
            session.session_id,
            ttl=ttl,
        )
        await self._redis.sadd(
            self._list_key(session.tenant_id, session.worker_id),
            session.session_id,
        )

    async def delete(self, session_id: str) -> None:
        session = await self.load(session_id)
        await self._redis.delete(self._session_key(session_id))
        if session is None:
            return
        await self._redis.delete(self._thread_key(session.thread_id))
        await self._redis.srem(
            self._list_key(session.tenant_id, session.worker_id),
            session_id,
        )

    async def find_by_thread(
        self,
        thread_id: str,
    ) -> Optional[ConversationSession]:
        session_id = await self._redis.get(self._thread_key(thread_id))
        if not session_id:
            return None
        session = await self.load(str(session_id))
        if session is None:
            await self._redis.delete(self._thread_key(thread_id))
        return session

    async def list_sessions(
        self,
        tenant_id: str,
        worker_id: str,
    ) -> tuple[ConversationSession, ...]:
        session_ids = sorted(
            str(value)
            for value in await self._redis.smembers(
                self._list_key(tenant_id, worker_id)
            )
            if str(value).strip()
        )
        if not session_ids:
            return ()

        raw_items = await self._redis.mget(
            *(self._session_key(session_id) for session_id in session_ids)
        )
        sessions: list[ConversationSession] = []
        stale_ids: list[str] = []
        for session_id, raw in zip(session_ids, raw_items):
            if raw is None:
                stale_ids.append(session_id)
                continue
            session = self._deserialize(raw, context=f"session {session_id}")
            if session is None:
                stale_ids.append(session_id)
                continue
            sessions.append(session)

        if stale_ids:
            await self._redis.srem(
                self._list_key(tenant_id, worker_id),
                *stale_ids,
            )

        return tuple(sessions)

    def _session_key(self, session_id: str) -> str:
        return f"{self._PREFIX}:{session_id}"

    def _thread_key(self, thread_id: str) -> str:
        return f"{self._THREAD_INDEX}:{thread_id}"

    def _list_key(self, tenant_id: str, worker_id: str) -> str:
        return f"{self._LIST_INDEX}:{tenant_id}:{worker_id}"

    def _deserialize(
        self,
        raw: str | None,
        *,
        context: str,
    ) -> Optional[ConversationSession]:
        if not raw:
            return None
        try:
            return ConversationSession.from_dict(json.loads(raw))
        except (TypeError, ValueError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("[RedisSessionStore] Failed to parse %s: %s", context, exc)
            return None


class HybridSessionStore:
    """File-ground-truth session store with Redis acceleration."""

    def __init__(
        self,
        redis_store: RedisSessionStore,
        file_store: FileSessionStore,
    ) -> None:
        self._redis = redis_store
        self._file = file_store

    async def load(self, session_id: str) -> Optional[ConversationSession]:
        try:
            session = await self._redis.load(session_id)
            if session is not None:
                return session
        except Exception as exc:
            logger.warning("[HybridSessionStore] Redis load failed: %s", exc)

        session = await self._file.load(session_id)
        await self._try_redis_backfill(session)
        return session

    async def save(self, session: ConversationSession) -> None:
        await self._file.save(session)
        try:
            await self._redis.save(session)
        except Exception as exc:
            logger.warning("[HybridSessionStore] Redis save failed: %s", exc)

    async def delete(self, session_id: str) -> None:
        try:
            await self._redis.delete(session_id)
        except Exception as exc:
            logger.warning("[HybridSessionStore] Redis delete failed: %s", exc)
        await self._file.delete(session_id)

    async def find_by_thread(
        self,
        thread_id: str,
    ) -> Optional[ConversationSession]:
        try:
            session = await self._redis.find_by_thread(thread_id)
            if session is not None:
                return session
        except Exception as exc:
            logger.warning(
                "[HybridSessionStore] Redis find_by_thread failed: %s",
                exc,
            )

        session = await self._file.find_by_thread(thread_id)
        await self._try_redis_backfill(session)
        return session

    async def list_sessions(
        self,
        tenant_id: str,
        worker_id: str,
    ) -> tuple[ConversationSession, ...]:
        try:
            sessions = await self._redis.list_sessions(tenant_id, worker_id)
            if sessions:
                return sessions
        except Exception as exc:
            logger.warning("[HybridSessionStore] Redis list_sessions failed: %s", exc)
        return await self._file.list_sessions(tenant_id, worker_id)

    async def _try_redis_backfill(
        self,
        session: ConversationSession | None,
    ) -> None:
        if session is None:
            return
        try:
            await self._redis.save(session)
        except Exception as exc:
            logger.warning("[HybridSessionStore] Redis backfill failed: %s", exc)
