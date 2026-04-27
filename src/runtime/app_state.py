"""FastAPI app.state wiring extracted from the API factory."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI

from src.common.logger import get_logger
from src.common.paths import resolve_workspace_root
from src.core.persona_reload_watcher import PersonaReloadWatcher
from src.runtime.default_worker_resolver import resolve_default_worker
from src.runtime.worker_reload import reload_worker_runtime_state

logger = get_logger()

if TYPE_CHECKING:
    from src.bootstrap.context import BootstrapContext


class MissingEngineDispatcher:
    """Explicit placeholder used when bootstrap wiring is incomplete."""

    async def dispatch(self, *args, **kwargs):
        raise RuntimeError(
            "engine_dispatcher not available in bootstrap context; "
            "worker routes cannot dispatch tasks"
        )
        yield  # pragma: no cover


def _build_fallback_worker_router(context: "BootstrapContext"):
    """Create a minimal WorkerRouter when bootstrap wiring is incomplete."""
    from src.common.tenant import TenantLoader
    from src.worker.registry import build_worker_registry
    from src.worker.router import WorkerRouter
    from src.worker.task import TaskStore
    from src.worker.task_runner import TaskRunner

    workspace_root = resolve_workspace_root(context.get_state("workspace_root"))
    tenant_loader = context.get_state(
        "tenant_loader",
        TenantLoader(workspace_root),
    )
    worker_registry = context.get_state(
        "worker_registry",
        build_worker_registry([]),
    )
    engine_dispatcher = context.get_state("engine_dispatcher")
    task_store = context.get_state("task_store", TaskStore(workspace_root))

    if engine_dispatcher is None:
        logger.warning(
            "[app_state] engine_dispatcher not in bootstrap context; "
            "worker routes will fail on dispatch"
        )
        engine_dispatcher = MissingEngineDispatcher()

    task_runner = TaskRunner(
        engine_dispatcher=engine_dispatcher,
        task_store=task_store,
    )
    return WorkerRouter(
        worker_registry=worker_registry,
        tenant_loader=tenant_loader,
        task_runner=task_runner,
    )


def store_dependencies(app: FastAPI, context: "BootstrapContext") -> None:
    """
    Extract dependencies from BootstrapContext and store them in app.state.

    This keeps the FastAPI factory thin while preserving the existing
    app.state contract consumed by routes and tests.
    """
    worker_router = context.get_state("worker_router")
    if worker_router is None:
        logger.warning(
            "[app_state] worker_router not found in bootstrap context. "
            "Creating a minimal WorkerRouter."
        )
        worker_router = _build_fallback_worker_router(context)

    app.state.worker_router = worker_router
    contact_registries = context.get_state("contact_registries", {})
    app.state.contact_registries = contact_registries
    if hasattr(worker_router, "_contact_registries"):
        worker_router._contact_registries = contact_registries
    app.state.worker_registry = context.get_state("worker_registry")
    app.state.worker_reload_status = context.get_state("worker_reload_status", {})
    app.state.trigger_managers = context.get_state("trigger_managers", {})
    app.state.worker_schedulers = context.get_state("worker_schedulers", {})
    app.state.heartbeat_runners = context.get_state("heartbeat_runners", {})
    app.state.main_session_runtimes = context.get_state("main_session_runtimes", {})
    app.state.sensor_registries = context.get_state("sensor_registries", {})
    app.state.im_channel_registry = context.get_state("im_channel_registry")
    app.state.channel_registry = context.get_state("channel_registry")
    app.state.channel_message_router = context.get_state("channel_message_router")
    app.state.channel_router = context.get_state("channel_router")
    app.state.channel_manager = context.get_state("channel_manager")
    app.state.isolated_run_manager = context.get_state("isolated_run_manager")
    app.state.engine_registry = context.get_state("engine_registry", {})
    app.state.langgraph_checkpointer = context.get_state("langgraph_checkpointer")
    app.state.langgraph_engine = context.get_state("langgraph_engine")
    app.state.session_inbox_store = context.get_state("session_inbox_store")
    app.state.goal_inbox_store = context.get_state("goal_inbox_store")
    app.state.integration_inbox_store = context.get_state("integration_inbox_store")
    app.state.integration_channel_gateway = context.get_state(
        "integration_channel_gateway"
    )
    app.state.workspace_root = str(resolve_workspace_root(context.get_state("workspace_root")))
    app.state.runtime_profile = getattr(context.settings, "runtime_profile", "local")
    app.state.settings = context.settings
    app.state.snapshot_runtime_components = context.snapshot_runtime_components
    app.state.runtime_component_requirements = (
        context.runtime_component_requirements()
    )
    app.state.resolve_default_worker = lambda: resolve_default_worker(context)
    engine_dispatcher = context.get_state("engine_dispatcher")
    session_inbox_store = context.get_state("session_inbox_store")
    session_search_index = context.get_state("session_search_index")
    for scheduler in app.state.worker_schedulers.values():
        replace_engine_dispatcher = getattr(scheduler, "replace_engine_dispatcher", None)
        if callable(replace_engine_dispatcher):
            replace_engine_dispatcher(engine_dispatcher)
        replace_inbox_store = getattr(scheduler, "replace_inbox_store", None)
        if callable(replace_inbox_store):
            replace_inbox_store(session_inbox_store)
    channel_message_router = context.get_state("channel_message_router")
    replace_runtime_dependencies = getattr(channel_message_router, "replace_runtime_dependencies", None)
    if callable(replace_runtime_dependencies):
        replace_runtime_dependencies(
            engine_dispatcher=engine_dispatcher,
            session_search_index=session_search_index,
        )

    session_manager = context.get_state("session_manager")
    if session_manager is not None:
        app.state.session_manager = session_manager
    if session_search_index is not None:
        app.state.session_search_index = session_search_index
        if hasattr(worker_router, "set_session_search_index"):
            worker_router.set_session_search_index(session_search_index)

    task_store = context.get_state("task_store")
    if task_store is not None:
        app.state.task_store = task_store

    task_spawner = context.get_state("task_spawner")
    if task_spawner is not None:
        app.state.task_spawner = task_spawner
        if hasattr(worker_router, "set_task_spawner"):
            worker_router.set_task_spawner(task_spawner)
    suggestion_store = context.get_state("suggestion_store")
    if suggestion_store is not None:
        app.state.suggestion_store = suggestion_store
    feedback_store = context.get_state("feedback_store")
    if feedback_store is not None:
        app.state.feedback_store = feedback_store
    goal_lock_registry = context.get_state("goal_lock_registry")
    if goal_lock_registry is not None:
        app.state.goal_lock_registry = goal_lock_registry
    lifecycle_services = context.get_state("lifecycle_services")
    if lifecycle_services is not None:
        app.state.lifecycle_services = lifecycle_services

    async def _reload_worker_runtime(
        worker_id: str,
        tenant_id: str,
        trigger_source: str = "manual",
        changed_files: tuple[str, ...] = (),
    ) -> dict:
        return await reload_worker_runtime_state(
            app=app,
            context=context,
            worker_router=getattr(app.state, "worker_router", worker_router),
            worker_id=worker_id,
            tenant_id=tenant_id,
            trigger_source=trigger_source,
            changed_files=changed_files,
        )

    app.state.reload_worker_runtime = _reload_worker_runtime
    app.state.bootstrap_context = context

    logger.info("[app_state] Dependencies stored in app.state")


def configure_persona_reload_watcher(
    app: FastAPI,
    settings,
) -> PersonaReloadWatcher | None:
    """Start the persona reload watcher when the feature is enabled."""
    if not getattr(settings, "persona_auto_reload_enabled", False):
        return None
    if getattr(app.state, "reload_worker_runtime", None) is None:
        return None

    watcher = PersonaReloadWatcher(
        workspace_root=resolve_workspace_root(getattr(app.state, "workspace_root", None)),
        reload_worker=app.state.reload_worker_runtime,
        interval_seconds=float(
            getattr(settings, "persona_auto_reload_interval_seconds", 2.0)
        ),
        debounce_seconds=float(
            getattr(settings, "persona_auto_reload_debounce_seconds", 1.0)
        ),
    )
    watcher.start()
    app.state.persona_reload_watcher = watcher
    logger.info("[app_state] Persona auto-reload watcher started")
    return watcher


async def stop_persona_reload_watcher(app: FastAPI) -> None:
    """Stop the watcher created by configure_persona_reload_watcher()."""
    watcher = getattr(app.state, "persona_reload_watcher", None)
    if watcher is not None:
        await watcher.stop()
