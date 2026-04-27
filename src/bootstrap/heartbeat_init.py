"""Bootstrap initializer for heartbeat cognition."""
from __future__ import annotations

from pathlib import Path

from src.autonomy.inbox import SessionInboxStore
from src.autonomy.isolated_run import IsolatedRunManager
from src.autonomy.main_session import MainSessionRuntime
from src.common.logger import get_logger
from src.common.runtime_status import aggregate_component_statuses
from src.worker.heartbeat.ledger import AttentionLedger
from src.worker.heartbeat.runner import HeartbeatRunner
from src.worker.heartbeat.strategy import HeartbeatStrategy, HeartbeatStrategyConfig

from .base import Initializer

logger = get_logger()


class HeartbeatInitializer(Initializer):
    """Register per-worker heartbeat runners into APScheduler."""

    def __init__(self) -> None:
        self._apscheduler = None
        self._runners: dict[str, HeartbeatRunner] = {}
        self._main_sessions: dict[str, MainSessionRuntime] = {}
        self._attention_ledgers: dict[str, AttentionLedger] = {}

    @property
    def name(self) -> str:
        return "heartbeat"

    @property
    def depends_on(self) -> list[str]:
        return ["scheduler", "conversation", "integrations"]

    @property
    def priority(self) -> int:
        return 135

    async def initialize(self, context) -> bool:
        self._apscheduler = context.get_state("apscheduler")
        worker_router = context.get_state("worker_router")
        worker_registry = context.get_state("worker_registry")
        session_manager = context.get_state("session_manager")
        task_store = context.get_state("task_store")
        worker_schedulers = context.get_state("worker_schedulers", {})
        event_bus = context.get_state("event_bus")
        tenant_id = context.get_state("tenant_id", "demo")
        redis_client = context.get_state("redis_client")
        workspace_root = Path(context.get_state("workspace_root", "workspace"))
        settings = context.settings

        if worker_registry is None or worker_router is None or session_manager is None:
            logger.warning(
                "[HeartbeatInit] Missing worker/session dependencies, skipping"
            )
            return True

        inbox_store = (
            context.get_state("session_inbox_store")
            or context.get_state("goal_inbox_store")
            or context.get_state("integration_inbox_store")
        )
        if inbox_store is None:
            inbox_store = SessionInboxStore(
                redis_client=redis_client,
                fallback_dir=workspace_root,
                event_bus=event_bus,
                processing_timeout_minutes=settings.heartbeat_processing_timeout_minutes,
            )
        strategy = HeartbeatStrategy(
            config=HeartbeatStrategyConfig.from_settings(settings)
        )
        isolated_run_manager = IsolatedRunManager(
            task_store=task_store,
            worker_schedulers=worker_schedulers,
            event_bus=event_bus,
        )
        base_strategy_config = HeartbeatStrategyConfig.from_settings(settings)

        for entry in worker_registry.list_all():
            worker_id = entry.worker.worker_id
            strategy = HeartbeatStrategy(
                config=base_strategy_config.with_worker_overrides(
                    entry.worker.heartbeat_config
                )
            )
            main_session = MainSessionRuntime(
                session_manager=session_manager,
                tenant_id=tenant_id,
                worker_id=worker_id,
                workspace_root=workspace_root,
                redis_client=redis_client,
            )
            if event_bus is not None:
                await main_session.start(event_bus)
            ledger = AttentionLedger(
                tenant_id=tenant_id,
                worker_id=worker_id,
                redis_client=redis_client,
                workspace_root=workspace_root,
            )

            runner = HeartbeatRunner(
                tenant_id=tenant_id,
                worker_id=worker_id,
                inbox_store=inbox_store,
                worker_router=worker_router,
                main_session_runtime=main_session,
                attention_ledger=ledger,
                worker_scheduler=worker_schedulers.get(worker_id),
                task_store=task_store,
                isolated_run_manager=isolated_run_manager,
                strategy=strategy,
            )
            if event_bus is not None:
                await runner.start(event_bus)
            if self._apscheduler is not None:
                self._apscheduler.add_job(
                    runner.run_once,
                    trigger="interval",
                    minutes=settings.heartbeat_interval_minutes,
                    args=[tenant_id, worker_id],
                    id=f"heartbeat:{worker_id}",
                    replace_existing=True,
                )
            self._runners[worker_id] = runner
            self._main_sessions[worker_id] = main_session
            self._attention_ledgers[worker_id] = ledger

        context.set_state("session_inbox_store", inbox_store)
        context.set_state("heartbeat_runners", dict(self._runners))
        context.set_state("main_session_runtimes", dict(self._main_sessions))
        context.set_state("attention_ledgers", dict(self._attention_ledgers))
        context.set_state("isolated_run_manager", isolated_run_manager)
        context.register_runtime_component(
            "inbox_store",
            inbox_store.runtime_status,
        )
        context.register_runtime_component(
            "main_session_meta",
            lambda: _aggregate_runtime_status(
                "main_session_meta",
                context.get_state("main_session_runtimes", {}).values(),
            ),
        )
        context.register_runtime_component(
            "attention_ledger",
            lambda: _aggregate_runtime_status(
                "attention_ledger",
                context.get_state("attention_ledgers", {}).values(),
            ),
        )
        logger.info(
            "[HeartbeatInit] Registered heartbeat for %s workers",
            len(self._runners),
        )
        return True

    async def cleanup(self) -> None:
        for worker_id, runner in self._runners.items():
            if self._apscheduler is not None:
                try:
                    self._apscheduler.remove_job(f"heartbeat:{worker_id}")
                except Exception:
                    pass
            await runner.stop()
        for runtime in self._main_sessions.values():
            await runtime.stop()
        self._runners.clear()
        self._main_sessions.clear()
        self._attention_ledgers.clear()


def _aggregate_runtime_status(component: str, runtimes) -> object:
    statuses = []
    for runtime in tuple(runtimes):
        provider = getattr(runtime, "runtime_status", None)
        if callable(provider):
            statuses.append(provider())
    return aggregate_component_statuses(component, tuple(statuses))
