"""Runtime reload orchestration extracted from the FastAPI app factory."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.common.paths import resolve_workspace_root
from src.common.time import utc_now_iso
from src.events.models import Event
from src.runtime.worker_refresh import (
    refresh_channel_registry as _refresh_channel_registry,
    refresh_contact_registry as _refresh_contact_registry,
    refresh_goal_health_checks as _refresh_goal_health_checks,
    refresh_worker_recurring_jobs as _refresh_worker_recurring_jobs,
    refresh_sensor_registry as _refresh_sensor_registry,
    refresh_trigger_manager as _refresh_trigger_manager,
)


async def reload_worker_runtime_state(
    *,
    app,
    context,
    worker_router,
    worker_id: str,
    tenant_id: str,
    trigger_source: str = "manual",
    changed_files: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Reload one worker and refresh dependent runtime state."""
    from src.worker.contacts.discovery import PersonExtractor
    from src.worker.loader import load_worker_entry
    from src.worker.heartbeat.strategy import (
        HeartbeatStrategy,
        HeartbeatStrategyConfig,
    )
    from src.worker.registry import replace_worker_entry
    from src.worker.trust_gate import compute_trust_gate
    from src.runtime.api_wiring import register_langgraph_approval_event_types

    workspace_root = resolve_workspace_root(getattr(app.state, "workspace_root", None))
    register_langgraph_approval_event_types(workspace_root)
    current_worker_router = getattr(app.state, "worker_router", worker_router)
    credential_loader = context.get_state("worker_channel_credential_loader")
    if credential_loader is not None and hasattr(credential_loader, "clear_cache"):
        credential_loader.clear_cache(tenant_id=tenant_id, worker_id=worker_id)
    platform_client_factory = context.get_state("platform_client_factory")
    if platform_client_factory is not None and hasattr(platform_client_factory, "invalidate"):
        platform_client_factory.invalidate(tenant_id=tenant_id, worker_id=worker_id)
    integration_channel_gateway = context.get_state("integration_channel_gateway")
    if integration_channel_gateway is not None and hasattr(integration_channel_gateway, "invalidate"):
        integration_channel_gateway.invalidate(tenant_id=tenant_id, worker_id=worker_id)

    worker_entry = load_worker_entry(
        workspace_root=workspace_root,
        tenant_id=tenant_id,
        worker_id=worker_id,
    )

    worker_registry = getattr(app.state, "worker_registry", None)
    if worker_registry is not None:
        updated_registry = replace_worker_entry(worker_registry, worker_entry)
        app.state.worker_registry = updated_registry
        context.set_state("worker_registry", updated_registry)
        if hasattr(current_worker_router, "_worker_registry"):
            current_worker_router._worker_registry = updated_registry

    tenant_loader = context.get_state("tenant_loader")
    if tenant_loader is not None:
        tenant = tenant_loader.load(tenant_id)
        trust_gates = dict(context.get_state("trust_gates", {}))
        trust_gates[worker_id] = compute_trust_gate(worker_entry.worker, tenant)
        context.set_state("trust_gates", trust_gates)

    contact_registry_refreshed = _refresh_contact_registry(
        app=app,
        context=context,
        worker_router=current_worker_router,
        worker_entry=worker_entry,
        tenant_id=tenant_id,
        workspace_root=workspace_root,
    )
    trigger_manager_refreshed = await _refresh_trigger_manager(
        app=app,
        context=context,
        worker_router=current_worker_router,
        worker_id=worker_id,
        tenant_id=tenant_id,
        workspace_root=workspace_root,
    )
    goal_checks_refreshed = await _refresh_goal_health_checks(
        app=app,
        context=context,
        worker_router=current_worker_router,
        worker_id=worker_id,
        tenant_id=tenant_id,
        workspace_root=workspace_root,
    )
    recurring_jobs_refreshed = _refresh_worker_recurring_jobs(
        context=context,
        worker_id=worker_id,
        tenant_id=tenant_id,
        workspace_root=workspace_root,
    )
    sensor_registry_refreshed = await _refresh_sensor_registry(
        app=app,
        context=context,
        worker_entry=worker_entry,
        worker_id=worker_id,
        tenant_id=tenant_id,
        workspace_root=workspace_root,
    )
    channel_registry_refreshed = await _refresh_channel_registry(
        app=app,
        context=context,
        worker_entry=worker_entry,
        worker_id=worker_id,
        tenant_id=tenant_id,
    )
    session_search_index = context.get_state(
        "session_search_index",
        getattr(app.state, "session_search_index", None),
    )
    if session_search_index is not None:
        app.state.session_search_index = session_search_index
        if hasattr(current_worker_router, "set_session_search_index"):
            current_worker_router.set_session_search_index(session_search_index)
    task_spawner = context.get_state(
        "task_spawner",
        getattr(app.state, "task_spawner", None),
    )
    if task_spawner is not None:
        app.state.task_spawner = task_spawner
        if hasattr(current_worker_router, "set_task_spawner"):
            current_worker_router.set_task_spawner(task_spawner)
    channel_router = getattr(app.state, "channel_message_router", None)
    if channel_router is not None and hasattr(channel_router, "replace_runtime_dependencies"):
        if hasattr(channel_router, "replace_contact_extractors"):
            contact_registries = getattr(app.state, "contact_registries", {}) or {}
            channel_router.replace_contact_extractors({
                refreshed_worker_id: PersonExtractor(contact_registry=registry)
                for refreshed_worker_id, registry in contact_registries.items()
            })
        if hasattr(channel_router, "replace_worker_router"):
            channel_router.replace_worker_router(current_worker_router)
        channel_router.replace_runtime_dependencies(
            suggestion_store=context.get_state("suggestion_store"),
            feedback_store=context.get_state("feedback_store"),
            inbox_store=(
                context.get_state("session_inbox_store")
                or context.get_state("goal_inbox_store")
                or context.get_state("integration_inbox_store")
            ),
            trigger_managers=getattr(app.state, "trigger_managers", {}) or {},
            worker_schedulers=getattr(app.state, "worker_schedulers", {}) or {},
            task_store=context.get_state("task_store"),
            llm_client=context.get_state("llm_client"),
            lifecycle_services=context.get_state("lifecycle_services"),
            session_search_index=session_search_index,
            engine_dispatcher=context.get_state("engine_dispatcher"),
        )
    if task_spawner is not None and hasattr(task_spawner, "replace_worker_schedulers"):
        task_spawner.replace_worker_schedulers(
            getattr(app.state, "worker_schedulers", {}) or {},
        )
    if task_spawner is not None and hasattr(task_spawner, "replace_worker_registry"):
        task_spawner.replace_worker_registry(
            getattr(app.state, "worker_registry", None),
        )
    isolated_run_manager = context.get_state("isolated_run_manager")
    if isolated_run_manager is not None and hasattr(
        isolated_run_manager,
        "replace_worker_schedulers",
    ):
        isolated_run_manager.replace_worker_schedulers(
            getattr(app.state, "worker_schedulers", {}) or {},
        )

    heartbeat_runners = getattr(app.state, "heartbeat_runners", {}) or {}
    worker_schedulers = getattr(app.state, "worker_schedulers", {}) or {}
    scheduler_inbox_store = (
        context.get_state("session_inbox_store")
        or context.get_state("goal_inbox_store")
        or context.get_state("integration_inbox_store")
    )
    engine_dispatcher = context.get_state("engine_dispatcher")
    for scheduler in worker_schedulers.values():
        if hasattr(scheduler, "replace_worker_router"):
            scheduler.replace_worker_router(current_worker_router)
        if hasattr(scheduler, "replace_engine_dispatcher"):
            scheduler.replace_engine_dispatcher(engine_dispatcher)
        if hasattr(scheduler, "replace_inbox_store"):
            scheduler.replace_inbox_store(scheduler_inbox_store)
    runner = heartbeat_runners.get(worker_id)
    for heartbeat_runner in heartbeat_runners.values():
        if hasattr(heartbeat_runner, "replace_worker_router"):
            heartbeat_runner.replace_worker_router(current_worker_router)
    if runner is not None:
        strategy = HeartbeatStrategy(
            config=HeartbeatStrategyConfig.from_settings(
                context.settings,
            ).with_worker_overrides(worker_entry.worker.heartbeat_config)
        )
        runner.update_strategy(strategy)
        if hasattr(runner, "replace_runtime_dependencies"):
            runner.replace_runtime_dependencies(
                worker_scheduler=(getattr(app.state, "worker_schedulers", {}) or {}).get(worker_id),
                isolated_run_manager=isolated_run_manager,
            )

    reload_metadata = _record_worker_reload_status(
        app=app,
        context=context,
        worker_id=worker_id,
        tenant_id=tenant_id,
        trigger_source=trigger_source,
        changed_files=changed_files,
    )

    if "CHANNEL_CREDENTIALS.json" in changed_files:
        event_bus = context.get_state("event_bus")
        if event_bus is not None:
            await event_bus.publish(Event(
                event_id=f"evt-reload-{worker_id}",
                type="worker.credentials_changed",
                source="runtime_reload",
                tenant_id=tenant_id,
                payload=(
                    ("tenant_id", tenant_id),
                    ("worker_id", worker_id),
                    ("changed_files", tuple(changed_files)),
                    ("trigger_source", trigger_source),
                ),
            ))

    return {
        "worker_id": worker_id,
        "name": worker_entry.worker.name,
        "heartbeat_config": {
            "goal_task_actions": list(worker_entry.worker.heartbeat_config.goal_task_actions),
            "goal_isolated_actions": list(worker_entry.worker.heartbeat_config.goal_isolated_actions),
            "goal_isolated_deviation_threshold": (
                worker_entry.worker.heartbeat_config.goal_isolated_deviation_threshold
            ),
        },
        "heartbeat_runner_refreshed": runner is not None,
        "contact_registry_refreshed": contact_registry_refreshed,
        "trigger_manager_refreshed": trigger_manager_refreshed,
        "goal_checks_refreshed": goal_checks_refreshed,
        "recurring_jobs_refreshed": recurring_jobs_refreshed,
        "sensor_registry_refreshed": sensor_registry_refreshed,
        "channel_registry_refreshed": channel_registry_refreshed,
        "reload_metadata": reload_metadata,
    }


def _record_worker_reload_status(
    *,
    app,
    context,
    worker_id: str,
    tenant_id: str,
    trigger_source: str,
    changed_files: tuple[str, ...],
) -> dict[str, Any]:
    worker_reload_status = dict(getattr(app.state, "worker_reload_status", {}) or {})
    metadata = {
        "tenant_id": tenant_id,
        "worker_id": worker_id,
        "trigger_source": str(trigger_source or "manual"),
        "changed_files": list(changed_files),
        "reloaded_at": utc_now_iso(),
    }
    worker_reload_status[(tenant_id, worker_id)] = metadata
    app.state.worker_reload_status = worker_reload_status
    context.set_state("worker_reload_status", dict(worker_reload_status))
    return metadata
