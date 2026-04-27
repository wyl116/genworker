"""
Scheduler bootstrap initializer.

Creates APScheduler + TriggerManagers + WorkerSchedulers.
Priority 110: after api_wiring(100), depends on events + workers.
"""
import logging
from pathlib import Path

from src.autonomy.inbox import SessionInboxStore
from src.common.runtime_status import aggregate_component_statuses
# Re-export runtime helpers here to preserve existing bootstrap imports.
from src.runtime.scheduler_runtime import (
    build_adopt_handler as _build_adopt_handler,
    build_duty_learning_handler as _build_duty_learning_handler,
    build_goal_check_prompt as _build_goal_check_prompt,
    build_goal_health_job_id,
    parse_interval_seconds as _parse_interval_seconds,
    register_goal_health_checks as _register_goal_health_checks,
    register_scheduler_workers as _register_scheduler_workers,
    register_single_goal_health_check as _register_single_goal_health_check,
    run_crystallization_cycle as _run_crystallization_cycle,
    run_goal_health_check,
    run_profile_update as _run_profile_update,
    run_sharing_cycle as _run_sharing_cycle,
)
from .base import Initializer

logger = logging.getLogger(__name__)


class SchedulerInitializer(Initializer):
    """
    Initialize the scheduling subsystem.

    Creates:
    - AsyncIOScheduler (MemoryJobStore)
    - TriggerManager per worker (registers duty triggers)
    - WorkerScheduler per worker (concurrency + quota)
    """

    def __init__(self) -> None:
        self._apscheduler = None
        self._trigger_managers: dict[str, object] = {}
        self._worker_schedulers: dict[str, object] = {}
        self._goal_inbox_store: SessionInboxStore | None = None

    @property
    def name(self) -> str:
        return "scheduler"

    @property
    def depends_on(self) -> list[str]:
        return ["events", "workers", "api_wiring"]

    @property
    def priority(self) -> int:
        return 110

    @property
    def required(self) -> bool:
        return False

    async def initialize(self, context) -> bool:
        """
        Create scheduler infrastructure.

        1. Create AsyncIOScheduler (MemoryJobStore)
        2. Start the scheduler
        3. For each worker: load duties, create TriggerManager, register triggers
        4. Create WorkerScheduler per worker
        5. Store in context
        """
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler

            from src.events.bus import EventBus

            self._apscheduler = AsyncIOScheduler()
            self._apscheduler.start()

            event_bus: EventBus | None = context.get_state("event_bus")
            worker_router = context.get_state("worker_router")
            worker_registry = context.get_state("worker_registry")
            workspace_root = Path(
                context.get_state("workspace_root", "workspace")
            )
            tenant_id = context.get_state("tenant_id", "demo")
            mcp_server = context.get_state("mcp_server")
            llm_client = context.get_state("llm_client")
            episode_lock = context.get_state("episode_lock")
            self._goal_inbox_store = SessionInboxStore(
                redis_client=None,
                fallback_dir=workspace_root,
                event_bus=event_bus,
                processing_timeout_minutes=
                getattr(context.settings, "heartbeat_processing_timeout_minutes", 10),
            )

            if event_bus is None:
                logger.warning(
                    "[SchedulerInit] EventBus not available, "
                    "skipping trigger registration"
                )
                context.set_state("apscheduler", self._apscheduler)
                return True

            if worker_registry is not None and worker_router is not None:
                (
                    self._trigger_managers,
                    self._worker_schedulers,
                ) = await _register_scheduler_workers(
                    apscheduler=self._apscheduler,
                    worker_registry=worker_registry,
                    worker_router=worker_router,
                    event_bus=event_bus,
                    workspace_root=workspace_root,
                    tenant_id=tenant_id,
                    redis_client=context.get_state("redis_client"),
                    mcp_server=mcp_server,
                    llm_client=llm_client,
                    episode_lock=episode_lock,
                    memory_orchestrator=context.get_state("memory_orchestrator"),
                    openviking_client=context.get_state("openviking_client"),
                    openviking_scope_prefix=getattr(
                        context.settings,
                        "openviking_scope_prefix",
                        "viking://",
                    ),
                    goal_inbox_store=self._goal_inbox_store,
                    suggestion_store=context.get_state("suggestion_store"),
                    feedback_store=context.get_state("feedback_store"),
                )

            context.set_state("apscheduler", self._apscheduler)
            context.set_state("trigger_managers", dict(self._trigger_managers))
            context.set_state(
                "worker_schedulers", dict(self._worker_schedulers)
            )
            context.set_state("goal_inbox_store", self._goal_inbox_store)
            context.register_runtime_component(
                "dead_letter_store",
                lambda: _aggregate_dead_letter_store_status(context),
            )

            logger.info(
                f"[SchedulerInit] Scheduler started with "
                f"{len(self._trigger_managers)} trigger managers"
            )
            return True

        except ImportError as exc:
            logger.warning(f"[SchedulerInit] APScheduler not available: {exc}")
            return False
        except Exception as exc:
            logger.error(f"[SchedulerInit] Failed: {exc}", exc_info=True)
            return False

    async def _register_all_workers(
        self,
        worker_registry,
        worker_router,
        event_bus,
        workspace_root: Path,
        tenant_id: str,
        redis_client,
        mcp_server,
        llm_client,
        episode_lock,
        memory_orchestrator=None,
        openviking_client=None,
        openviking_scope_prefix: str = "viking://",
    ) -> None:
        """Register triggers and schedulers for all workers."""
        (
            self._trigger_managers,
            self._worker_schedulers,
        ) = await _register_scheduler_workers(
            apscheduler=self._apscheduler,
            worker_registry=worker_registry,
            worker_router=worker_router,
            event_bus=event_bus,
            workspace_root=workspace_root,
            tenant_id=tenant_id,
            redis_client=redis_client,
            mcp_server=mcp_server,
            llm_client=llm_client,
            episode_lock=episode_lock,
            memory_orchestrator=memory_orchestrator,
            openviking_client=openviking_client,
            openviking_scope_prefix=openviking_scope_prefix,
            goal_inbox_store=self._goal_inbox_store,
        )

    async def _register_goal_checks(
        self,
        scheduler,
        goals_dir: Path,
        tenant_id: str,
        worker_id: str,
        worker_scheduler,
        event_bus,
        workspace_root: Path | None = None,
    ) -> None:
        """Register periodic goal health checks for active goals."""
        await _register_goal_health_checks(
            scheduler=scheduler,
            goals_dir=goals_dir,
            tenant_id=tenant_id,
            worker_id=worker_id,
            worker_scheduler=worker_scheduler,
            event_bus=event_bus,
            inbox_store=self._goal_inbox_store,
            workspace_root=workspace_root,
        )

    async def cleanup(self) -> None:
        """Shutdown scheduler and unregister all triggers."""
        for wid, mgr in self._trigger_managers.items():
            try:
                for duty_id in list(mgr.registered_duties):
                    await mgr.unregister_duty(duty_id)
            except Exception as exc:
                logger.error(
                    f"[SchedulerInit] Failed to unregister triggers "
                    f"for worker '{wid}': {exc}"
                )

        if self._apscheduler is not None:
            try:
                self._apscheduler.shutdown(wait=False)
            except Exception:
                pass
            logger.info("[SchedulerInit] APScheduler shut down")

        self._trigger_managers.clear()
        self._worker_schedulers.clear()

    async def _run_goal_health_check(
        self,
        goal_file: Path,
        tenant_id: str,
        worker_id: str,
        worker_scheduler,
        event_bus,
        inbox_store: SessionInboxStore | None = None,
        goal_id: str = "",
        workspace_root: Path | None = None,
    ) -> None:
        """Load a goal, evaluate progress, and enqueue remediation if needed."""
        await run_goal_health_check(
            goal_file=goal_file,
            tenant_id=tenant_id,
            worker_id=worker_id,
            worker_scheduler=worker_scheduler,
            event_bus=event_bus,
            inbox_store=inbox_store,
            goal_id=goal_id,
            workspace_root=workspace_root,
        )


def _aggregate_dead_letter_store_status(context):
    statuses = []
    for scheduler in (context.get_state("worker_schedulers", {}) or {}).values():
        store = getattr(getattr(scheduler, "_side_effects", None), "_dead_letter_store", None)
        runtime_status = getattr(store, "runtime_status", None)
        if callable(runtime_status):
            statuses.append(runtime_status())
    return aggregate_component_statuses("dead_letter_store", tuple(statuses))
