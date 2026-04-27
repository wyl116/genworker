"""
ConversationInitializer - bootstrap initializer for conversation subsystem.

Sets up SessionManager, FileSessionStore, TaskSpawner,
and EventBus task.failed subscription.
"""
from pathlib import Path
from typing import Optional

from src.common.logger import get_logger
from src.common.runtime_status import ComponentRuntimeStatus, ComponentStatus

from .base import Initializer
from .context import BootstrapContext

logger = get_logger()


class ConversationInitializer(Initializer):
    """
    Bootstrap initializer for the conversation subsystem.

    Priority 120: runs after api_wiring (100) and scheduler (110).
    Depends on: api_wiring (WorkerRouter, TaskStore), events (EventBus).
    """

    def __init__(self) -> None:
        self._subscription_id: Optional[str] = None
        self._session_manager = None

    @property
    def name(self) -> str:
        return "conversation"

    @property
    def depends_on(self) -> list[str]:
        return ["api_wiring", "events", "scheduler"]

    @property
    def priority(self) -> int:
        return 120

    @property
    def required(self) -> bool:
        return False

    async def initialize(self, context: BootstrapContext) -> bool:
        """
        Initialize conversation subsystem.

        Steps:
        1. Create FileSessionStore
        2. Create SessionManager
        3. Create TaskSpawner
        4. Subscribe to task.failed events via EventBus
        5. Store session_manager and task_spawner in context
        """
        try:
            workspace_root = Path(
                context.get_state("workspace_root", "workspace")
            )

            # 1. Create session store
            from src.conversation.session_store import (
                FileSessionStore,
                HybridSessionStore,
                RedisSessionStore,
            )
            file_store = FileSessionStore(workspace_root)
            redis_client = context.get_state("redis_client")
            if redis_client is not None:
                store = HybridSessionStore(
                    RedisSessionStore(redis_client),
                    file_store,
                )
                session_store_status = ComponentRuntimeStatus(
                    component="session_store",
                    enabled=True,
                    status=ComponentStatus.READY,
                    selected_backend="hybrid",
                    primary_backend="redis",
                    fallback_backend="file",
                    ground_truth="file",
                )
                logger.info(
                    "[ConversationInit] component=session_store status=ready backend=hybrid ground_truth=file primary=redis"
                )
            else:
                store = file_store
                session_store_status = ComponentRuntimeStatus(
                    component="session_store",
                    enabled=True,
                    status=ComponentStatus.READY,
                    selected_backend="file",
                    ground_truth="file",
                )
                logger.info(
                    "[ConversationInit] component=session_store status=ready backend=file ground_truth=file"
                )

            # 2. Create SessionManager
            from src.conversation.session_manager import SessionManager
            from src.conversation.search_index import SessionSearchIndex

            search_index = SessionSearchIndex(
                db_path=str(workspace_root / "session_search.db")
            )
            await search_index.initialize()

            session_manager = SessionManager(store=store, search_index=search_index)
            memory_orchestrator = context.get_state("memory_orchestrator")
            if memory_orchestrator is not None:
                session_manager.set_memory_orchestrator(memory_orchestrator)
            self._session_manager = session_manager
            worker_router = context.get_state("worker_router")
            if worker_router is not None and hasattr(worker_router, "set_session_search_index"):
                worker_router.set_session_search_index(search_index)

            # 3. Create TaskSpawner
            from src.conversation.task_spawner import TaskSpawner
            task_store = context.get_state("task_store")
            skill_registry = context.get_state("skill_registry")
            worker_registry = context.get_state("worker_registry")
            worker_schedulers = context.get_state("worker_schedulers", {})
            event_bus = context.get_state("event_bus")

            task_spawner = TaskSpawner(
                task_store=task_store,
                skill_registry=skill_registry,
                worker_registry=worker_registry,
                worker_schedulers=worker_schedulers,
                event_bus=event_bus,
            )
            if worker_router is not None and hasattr(worker_router, "set_task_spawner"):
                worker_router.set_task_spawner(task_spawner)

            # 4. Subscribe to task.failed via EventBus
            event_bus = context.get_state("event_bus")
            if event_bus is not None:
                self._subscription_id = _subscribe_task_failed(
                    event_bus, session_manager,
                )

            # 5. Store in context
            context.set_state("session_manager", session_manager)
            context.set_state("task_spawner", task_spawner)
            context.set_state("session_search_index", search_index)
            context.register_runtime_component(
                "session_store",
                lambda: session_store_status,
                required=self.required,
            )

            logger.info(
                "[ConversationInit] Conversation subsystem initialized"
            )
            return True

        except Exception as exc:
            logger.error(
                f"[ConversationInit] Failed: {exc}", exc_info=True,
            )
            context.record_error("conversation", str(exc))
            return False

    async def cleanup(self) -> None:
        """Clean up expired sessions."""
        if self._session_manager is not None:
            try:
                cleaned = await self._session_manager.cleanup_expired()
                if cleaned > 0:
                    logger.info(
                        f"[ConversationInit] Cleaned {cleaned} "
                        f"expired sessions during shutdown"
                    )
            except Exception as exc:
                logger.error(
                    f"[ConversationInit] Cleanup error: {exc}"
                )


def _subscribe_task_failed(event_bus, session_manager) -> str:
    """Subscribe to task.failed events on the EventBus."""
    from src.events.bus import Subscription

    async def _on_task_failed(event) -> None:
        """Handle task.failed events by recording failure in SessionManager."""
        payload_dict = dict(event.payload)
        session_id = payload_dict.get("session_id", "")
        error_message = payload_dict.get("error_message", "Unknown error")
        task_id = payload_dict.get("task_id", "")

        if session_id:
            session_manager.record_task_failure(
                session_id=session_id,
                error_message=f"Task {task_id}: {error_message}",
            )

    subscription = Subscription(
        handler_id="conversation_task_failed_handler",
        event_type="task.failed",
        tenant_id="*",
        handler=_on_task_failed,
    )
    return event_bus.subscribe(subscription)
