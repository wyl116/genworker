"""Main session runtime for heartbeat cognition."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from src.common.logger import get_logger
from src.common.runtime_status import ComponentRuntimeStatus, ComponentStatus
from src.common.time import utc_now_iso
from src.conversation.models import ChatMessage, ConversationSession
from src.conversation.session_manager import SessionManager
from src.events.models import Event, EventBusProtocol, Subscription

logger = get_logger()


class MainSessionRuntime:
    """Manage the long-lived main session and heartbeat metadata."""

    def __init__(
        self,
        *,
        session_manager: SessionManager,
        tenant_id: str,
        worker_id: str,
        workspace_root: Path | str = "workspace",
        redis_client: Any | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._tenant_id = tenant_id
        self._worker_id = worker_id
        self._workspace_root = Path(workspace_root)
        self._redis = redis_client
        self._event_bus: EventBusProtocol | None = None
        self._subscription_ids: list[str] = []
        self._session_key = f"main:{worker_id}"
        self._status = ComponentStatus.READY
        self._last_error = ""
        self._selected_backend = "redis" if redis_client is not None else "file"

    @property
    def session_key(self) -> str:
        return self._session_key

    def runtime_status(self) -> ComponentRuntimeStatus:
        return ComponentRuntimeStatus(
            component="main_session_meta",
            enabled=True,
            status=self._status,
            selected_backend=self._selected_backend,
            primary_backend="redis",
            fallback_backend="file",
            ground_truth="file",
            last_error=self._last_error,
        )

    async def start(self, event_bus: EventBusProtocol) -> None:
        self._event_bus = event_bus
        subscriptions = (
            Subscription(
                handler_id=f"main_session:{self._worker_id}:isolated_run_completed",
                event_type="isolated_run.completed",
                tenant_id=self._tenant_id,
                handler=self._on_isolated_run_completed,
            ),
            Subscription(
                handler_id=f"main_session:{self._worker_id}:isolated_run_failed",
                event_type="isolated_run.failed",
                tenant_id=self._tenant_id,
                handler=self._on_isolated_run_failed,
            ),
            Subscription(
                handler_id=f"main_session:{self._worker_id}:task_completed",
                event_type="task.completed",
                tenant_id=self._tenant_id,
                handler=self._on_task_completed,
            ),
            Subscription(
                handler_id=f"main_session:{self._worker_id}:task_failed",
                event_type="task.failed",
                tenant_id=self._tenant_id,
                handler=self._on_task_failed,
            ),
        )
        for subscription in subscriptions:
            self._subscription_ids.append(event_bus.subscribe(subscription))

    async def stop(self) -> None:
        if self._event_bus is not None:
            for handler_id in self._subscription_ids:
                self._event_bus.unsubscribe(self._tenant_id, handler_id)
        self._subscription_ids.clear()

    async def get_session(self) -> ConversationSession:
        session = await self._session_manager.get_or_create(
            thread_id=self._session_key,
            tenant_id=self._tenant_id,
            worker_id=self._worker_id,
            session_type="main",
            main_session_key=self._session_key,
        )
        meta = await self._load_meta()
        session = replace(
            session,
            session_type="main",
            main_session_key=self._session_key,
            inbox_cursor=meta.get("inbox_cursor"),
            last_heartbeat_at=meta.get("last_heartbeat_at"),
            open_concerns=tuple(meta.get("open_concerns", [])),
            task_refs=tuple(meta.get("task_refs", [])),
        )
        await self._session_manager.save(session)
        return session

    async def append_message(self, message: ChatMessage) -> ConversationSession:
        session = await self.get_session()
        session = session.append_message(message)
        await self._session_manager.save(session)
        return session

    async def build_task_context(self, max_messages: int = 8) -> str:
        session = await self.get_session()
        if not session.messages:
            return ""
        recent = session.messages[-max_messages:]
        return "\n".join(f"{msg.role}: {msg.content}" for msg in recent)

    async def update_heartbeat_state(
        self,
        *,
        inbox_cursor: str | None = None,
        open_concerns: tuple[str, ...] | None = None,
        task_refs: tuple[str, ...] | None = None,
        last_heartbeat_at: str | None = None,
    ) -> ConversationSession:
        session = await self.get_session()
        meta = await self._load_meta()
        if inbox_cursor is not None:
            meta["inbox_cursor"] = inbox_cursor
        if open_concerns is not None:
            meta["open_concerns"] = list(open_concerns)
        if task_refs is not None:
            meta["task_refs"] = list(task_refs)
        meta["last_heartbeat_at"] = last_heartbeat_at or utc_now_iso()
        await self._save_meta(meta)

        session = replace(
            session,
            inbox_cursor=meta.get("inbox_cursor"),
            last_heartbeat_at=meta.get("last_heartbeat_at"),
            open_concerns=tuple(meta.get("open_concerns", [])),
            task_refs=tuple(meta.get("task_refs", [])),
        )
        await self._session_manager.save(session)
        return session

    async def _on_isolated_run_completed(self, event: Event) -> None:
        payload = dict(event.payload)
        if payload.get("main_session_key") != self._session_key:
            return
        run_id = payload.get("run_id", "")
        summary = payload.get("summary", "")
        await self.append_message(
            ChatMessage(role="assistant", content=f"[IsolatedRun {run_id} 完成] {summary}")
        )

    async def _on_isolated_run_failed(self, event: Event) -> None:
        payload = dict(event.payload)
        if payload.get("main_session_key") != self._session_key:
            return
        run_id = payload.get("run_id", "")
        task_id = payload.get("task_id", "")
        error = payload.get("error_message", "unknown")
        label = run_id or task_id or "unknown"
        await self.append_message(
            ChatMessage(role="system", content=f"[IsolatedRun {label} 失败] {error}")
        )

    async def _on_task_completed(self, event: Event) -> None:
        payload = dict(event.payload)
        if payload.get("thread_id") != self._session_key:
            return
        task_id = str(payload.get("task_id", "")).strip()
        description = str(payload.get("description", "")).strip()
        summary = str(payload.get("summary", "")).strip()
        label = description or task_id or "后台任务"
        content = f"[Task 完成] {label}"
        if summary:
            content = f"{content}\n{summary}"
        await self.append_message(ChatMessage(role="assistant", content=content))

    async def _on_task_failed(self, event: Event) -> None:
        payload = dict(event.payload)
        if payload.get("thread_id") != self._session_key:
            return
        task_id = str(payload.get("task_id", "")).strip()
        error = str(payload.get("error_message", "")).strip() or "unknown"
        label = task_id or "后台任务"
        await self.append_message(
            ChatMessage(role="system", content=f"[Task 失败] {label}: {error}")
        )

    async def _load_meta(self) -> dict[str, Any]:
        if self._redis is not None:
            try:
                return {
                    "inbox_cursor": await self._redis.get(self._cursor_key()),
                    "last_heartbeat_at": await self._redis.get(self._heartbeat_key()),
                    "open_concerns": await self._redis.get_json(self._concerns_key()) or [],
                    "task_refs": sorted(await self._redis.smembers(self._task_refs_key())),
                }
            except Exception as exc:
                self._mark_fallback(exc)
                logger.warning("[MainSessionRuntime] Redis meta read failed: %s", exc)
        meta_file = self._meta_file()
        if not meta_file.is_file():
            return {
                "inbox_cursor": None,
                "last_heartbeat_at": None,
                "open_concerns": [],
                "task_refs": [],
            }
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[MainSessionRuntime] Failed to read meta file: %s", exc)
            return {
                "inbox_cursor": None,
                "last_heartbeat_at": None,
                "open_concerns": [],
                "task_refs": [],
            }

    async def _save_meta(self, meta: dict[str, Any]) -> None:
        if self._redis is not None:
            try:
                if meta.get("inbox_cursor") is not None:
                    await self._redis.set(self._cursor_key(), str(meta["inbox_cursor"]))
                if meta.get("last_heartbeat_at") is not None:
                    await self._redis.set(
                        self._heartbeat_key(), str(meta["last_heartbeat_at"])
                    )
                await self._redis.set_json(
                    self._concerns_key(), list(meta.get("open_concerns", []))
                )
                await self._redis.delete(self._task_refs_key())
                task_refs = list(meta.get("task_refs", []))
                if task_refs:
                    await self._redis.sadd(self._task_refs_key(), *task_refs)
                return
            except Exception as exc:
                self._mark_fallback(exc)
                logger.warning("[MainSessionRuntime] Redis meta write failed: %s", exc)
        meta_file = self._meta_file()
        meta_file.parent.mkdir(parents=True, exist_ok=True)
        meta_file.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _meta_file(self) -> Path:
        return (
            self._workspace_root
            / "tenants"
            / self._tenant_id
            / "workers"
            / self._worker_id
            / "runtime"
            / "heartbeat_meta.json"
        )

    def _cursor_key(self) -> str:
        return f"main_session:{self._tenant_id}:{self._worker_id}:cursor"

    def _concerns_key(self) -> str:
        return f"main_session:{self._tenant_id}:{self._worker_id}:concerns"

    def _mark_fallback(self, exc: Exception) -> None:
        self._status = ComponentStatus.DEGRADED
        self._selected_backend = "file"
        self._last_error = str(exc).splitlines()[0][:200]

    def _heartbeat_key(self) -> str:
        return f"main_session:{self._tenant_id}:{self._worker_id}:heartbeat"

    def _task_refs_key(self) -> str:
        return f"main_session:{self._tenant_id}:{self._worker_id}:task_refs"
