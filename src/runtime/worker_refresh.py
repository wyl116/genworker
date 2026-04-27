"""Sub-system refresh helpers used by worker runtime reload."""
from __future__ import annotations

from pathlib import Path


def refresh_contact_registry(
    *,
    app,
    context,
    worker_router,
    worker_entry,
    tenant_id: str,
    workspace_root: Path,
) -> bool:
    from src.worker.contacts import ContactRegistry

    event_bus = context.get_state("event_bus")
    worker_id = worker_entry.worker.worker_id
    worker_contacts_dir = (
        workspace_root / "tenants" / tenant_id / "workers" / worker_id / "contacts"
    )
    registry = ContactRegistry(
        worker_contacts_dir,
        event_bus=event_bus,
        config=worker_entry.worker.contacts_config,
    )
    if worker_entry.worker.configured_contacts:
        registry.bootstrap_configured(worker_entry.worker.configured_contacts)

    contact_registries = dict(context.get_state("contact_registries", {}) or {})
    contact_registries[worker_id] = registry
    app.state.contact_registries = contact_registries
    context.set_state("contact_registries", contact_registries)
    if hasattr(worker_router, "_contact_registries"):
        worker_router._contact_registries = contact_registries
    return True


async def refresh_trigger_manager(
    *,
    app,
    context,
    worker_router,
    worker_id: str,
    tenant_id: str,
    workspace_root: Path,
) -> bool:
    from src.runtime.scheduler_runtime import build_duty_learning_handler
    from src.runtime.scheduler_runtime import load_unique_duties
    from src.worker.duty.duty_executor import DutyExecutor
    from src.worker.duty.trigger_manager import TriggerManager
    from src.worker.trust_gate import compute_trust_gate

    apscheduler = context.get_state("apscheduler")
    event_bus = context.get_state("event_bus")
    if apscheduler is None or event_bus is None or worker_router is None:
        return False

    trigger_managers = dict(getattr(app.state, "trigger_managers", {}) or {})
    old_manager = trigger_managers.get(worker_id)
    if old_manager is not None:
        for duty_id in list(getattr(old_manager, "registered_duties", ())):
            await old_manager.unregister_duty(duty_id)

    worker_dir = workspace_root / "tenants" / tenant_id / "workers" / worker_id
    worker_entry = worker_router._worker_registry.get(worker_id)
    tenant_loader = getattr(worker_router, "_tenant_loader", None) or context.get_state("tenant_loader")
    trust_gate = None
    if worker_entry is not None and tenant_loader is not None:
        try:
            tenant = tenant_loader.load(tenant_id)
            trust_gate = compute_trust_gate(worker_entry.worker, tenant)
        except Exception:
            trust_gate = None
    trigger_manager = TriggerManager(
        apscheduler,
        event_bus,
        DutyExecutor(
            worker_router,
            worker_dir / "duties",
            duty_learning_handler=build_duty_learning_handler(
                worker_dir=worker_dir,
                llm_client=context.get_state("llm_client"),
                episode_lock=context.get_state("episode_lock"),
                memory_orchestrator=context.get_state("memory_orchestrator"),
                openviking_client=context.get_state("openviking_client"),
                openviking_scope_prefix=getattr(
                    context.settings,
                    "openviking_scope_prefix",
                    "viking://",
                ),
                trust_gate=trust_gate,
            ),
        ),
    )

    duties_dir = worker_dir / "duties"
    for duty in load_unique_duties(duties_dir):
        if duty.status == "active":
            await trigger_manager.register_duty(duty, tenant_id, worker_id)

    trigger_managers[worker_id] = trigger_manager
    app.state.trigger_managers = trigger_managers
    context.set_state("trigger_managers", dict(trigger_managers))
    return True


async def refresh_goal_health_checks(
    *,
    app,
    context,
    worker_router,
    worker_id: str,
    tenant_id: str,
    workspace_root: Path,
) -> bool:
    apscheduler = context.get_state("apscheduler")
    if apscheduler is None:
        return False

    from apscheduler.triggers.interval import IntervalTrigger

    from src.runtime.scheduler_runtime import (
        build_goal_health_job_id,
        load_unique_goals,
        parse_interval_seconds,
        run_goal_health_check,
    )
    from src.worker.dead_letter import DeadLetterStore
    from src.worker.scheduler import SchedulerConfig, WorkerScheduler

    worker_schedulers = dict(getattr(app.state, "worker_schedulers", {}) or {})
    worker_scheduler = worker_schedulers.get(worker_id)
    if worker_scheduler is None and worker_router is not None:
        worker_scheduler = WorkerScheduler(
            config=SchedulerConfig(),
            worker_router=worker_router,
            event_bus=context.get_state("event_bus"),
            dead_letter_store=DeadLetterStore(
                redis_client=context.get_state("redis_client"),
                fallback_dir=workspace_root,
            ),
        )
        worker_schedulers[worker_id] = worker_scheduler
        app.state.worker_schedulers = worker_schedulers
        context.set_state("worker_schedulers", dict(worker_schedulers))

    if worker_scheduler is None:
        return False

    get_jobs = getattr(apscheduler, "get_jobs", None)
    if callable(get_jobs):
        for job in list(get_jobs()):
            job_id = str(getattr(job, "id", ""))
            job_args = tuple(getattr(job, "args", ()) or ())
            if is_goal_job_for_worker(job_id, job_args, worker_id):
                try:
                    apscheduler.remove_job(job_id)
                except Exception:
                    pass

    event_bus = context.get_state("event_bus")
    inbox_store = (
        context.get_state("session_inbox_store")
        or context.get_state("goal_inbox_store")
        or context.get_state("integration_inbox_store")
    )
    goals_dir = workspace_root / "tenants" / tenant_id / "workers" / worker_id / "goals"
    if not goals_dir.is_dir():
        return True

    for goal_file, goal in load_unique_goals(goals_dir):
        if goal.status != "active":
            continue

        interval_seconds = parse_interval_seconds(
            goal.external_source.sync_schedule
            if goal.external_source and goal.external_source.sync_schedule
            else "1h"
        )
        apscheduler.add_job(
            run_goal_health_check,
            trigger=IntervalTrigger(seconds=interval_seconds),
            id=build_goal_health_job_id(worker_id, goal.goal_id),
            args=(
                goal_file,
                tenant_id,
                worker_id,
                worker_scheduler,
                event_bus,
                inbox_store,
                goal.goal_id,
                workspace_root,
            ),
            replace_existing=True,
        )
    return True


def refresh_worker_recurring_jobs(
    *,
    context,
    worker_id: str,
    tenant_id: str,
    workspace_root: Path,
) -> bool:
    apscheduler = context.get_state("apscheduler")
    if apscheduler is None:
        return False

    from src.runtime.scheduler_runtime import (
        register_worker_recurring_jobs,
        remove_worker_recurring_jobs,
    )

    remove_worker_recurring_jobs(scheduler=apscheduler, worker_id=worker_id)

    trust_gate = None
    trust_gates = context.get_state("trust_gates", {}) or {}
    if isinstance(trust_gates, dict):
        trust_gate = trust_gates.get(worker_id)

    register_worker_recurring_jobs(
        scheduler=apscheduler,
        tenant_id=tenant_id,
        worker_id=worker_id,
        worker_dir=workspace_root / "tenants" / tenant_id / "workers" / worker_id,
        workspace_root=workspace_root,
        mcp_server=context.get_state("mcp_server"),
        llm_client=context.get_state("llm_client"),
        goal_inbox_store=(
            context.get_state("session_inbox_store")
            or context.get_state("goal_inbox_store")
            or context.get_state("integration_inbox_store")
        ),
        suggestion_store=context.get_state("suggestion_store"),
        feedback_store=context.get_state("feedback_store"),
        cross_worker_sharing_enabled=bool(
            getattr(trust_gate, "cross_worker_sharing_enabled", False)
        ),
    )
    return True


async def refresh_sensor_registry(
    *,
    app,
    context,
    worker_entry,
    worker_id: str,
    tenant_id: str,
    workspace_root: Path,
) -> bool:
    from src.worker.sensing import SensorRegistry, SnapshotStore, parse_sensor_config
    from src.worker.sensing.factory import create_sensor

    sensor_registries = dict(getattr(app.state, "sensor_registries", {}) or {})
    existing_registry = sensor_registries.get(worker_id)
    if existing_registry is not None:
        await existing_registry.stop_all()

    if not worker_entry.worker.sensor_configs:
        sensor_registries.pop(worker_id, None)
        app.state.sensor_registries = sensor_registries
        context.set_state("sensor_registries", dict(sensor_registries))
        return existing_registry is not None

    inbox_store = (
        context.get_state("session_inbox_store")
        or context.get_state("integration_inbox_store")
    )
    overrides: dict[str, str] = {}
    for raw in worker_entry.worker.sensor_configs:
        override = str(raw.get("cognition_route_override", "")).strip()
        if override:
            overrides[str(raw.get("source_type", ""))] = override

    registry = SensorRegistry(
        tenant_id=tenant_id,
        worker_id=worker_id,
        inbox_store=inbox_store,
        event_bus=context.get_state("event_bus"),
        scheduler=context.get_state("apscheduler"),
        snapshot_store=SnapshotStore(
            workspace_root=workspace_root,
            tenant_id=tenant_id,
            worker_id=worker_id,
        ),
        cognition_route_overrides=overrides or None,
    )
    deps = {
        "tenant_id": tenant_id,
        "platform_client_factory": context.get_state("platform_client_factory"),
        "redis_client": context.get_state("redis_client"),
        "tool_executor": context.get_state("tool_executor"),
        "mount_manager": context.get_state("mount_manager"),
        "message_deduplicator": context.get_state("message_deduplicator"),
    }
    for raw in worker_entry.worker.sensor_configs:
        config = parse_sensor_config(raw)
        try:
            sensor = create_sensor(config, worker_id=worker_id, **deps)
        except ValueError:
            continue
        await registry.register(sensor, config)

    sensor_registries[worker_id] = registry
    app.state.sensor_registries = sensor_registries
    context.set_state("sensor_registries", dict(sensor_registries))
    channel_router = getattr(app.state, "channel_message_router", None)
    if channel_router is not None and hasattr(channel_router, "replace_sensor_registries"):
        channel_router.replace_sensor_registries(dict(sensor_registries))
    return True


async def refresh_channel_registry(
    *,
    app,
    context,
    worker_entry,
    worker_id: str,
    tenant_id: str,
) -> bool:
    from src.channels.bindings import build_worker_bindings

    channel_manager = getattr(app.state, "channel_manager", None)
    if channel_manager is None or not hasattr(channel_manager, "reload_worker"):
        return False

    bindings = tuple(build_worker_bindings(
        worker_entry,
        tenant_id=tenant_id,
        platform_client_factory=context.get_state("platform_client_factory"),
    ))
    await channel_manager.reload_worker(tenant_id, worker_id, bindings)

    registry = getattr(app.state, "im_channel_registry", None)
    router = getattr(app.state, "channel_message_router", None)
    context.set_state("im_channel_registry", registry)
    context.set_state("channel_registry", registry)
    context.set_state("channel_message_router", router)
    context.set_state("channel_router", router)
    context.set_state("channel_manager", channel_manager)
    app.state.im_channel_registry = registry
    app.state.channel_registry = registry
    app.state.channel_message_router = router
    app.state.channel_router = router
    app.state.channel_manager = channel_manager
    return True


def is_goal_job_for_worker(job_id: str, job_args: tuple, worker_id: str) -> bool:
    if job_id.startswith(f"goal:{worker_id}:"):
        return True
    return (
        job_id.startswith("goal:")
        and len(job_args) >= 3
        and str(job_args[2]) == worker_id
    )
