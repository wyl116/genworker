"""Scheduler runtime helpers extracted from bootstrap initialization."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from src.autonomy.inbox import InboxItem, SessionInboxStore
from src.events.models import Event
from src.worker.lifecycle.duty_skill_detector import run_duty_skill_detection
from src.worker.scripts.models import serialize_pre_script

logger = logging.getLogger(__name__)


def build_worker_recurring_job_ids(worker_id: str) -> dict[str, str]:
    """Build the canonical recurring scheduler job ids for one worker."""
    return {
        "profile_update": f"system:profile-update:{worker_id}",
        "crystallization": f"system:crystallization:{worker_id}",
        "task_pattern": f"system:task-pattern:{worker_id}",
        "goal_completion": f"system:goal-completion-advisor:{worker_id}",
        "duty_drift": f"system:duty-drift:{worker_id}",
        "sharing_cycle": f"system:sharing-cycle:{worker_id}",
        "duty_to_skill": f"system:duty-to-skill:{worker_id}",
    }


def remove_worker_recurring_jobs(*, scheduler, worker_id: str) -> None:
    """Remove all recurring non-goal scheduler jobs for one worker."""
    for job_id in build_worker_recurring_job_ids(worker_id).values():
        try:
            scheduler.remove_job(job_id)
        except Exception:
            continue


def load_unique_duties(duties_dir: Path) -> tuple:
    """Load canonical duties from markdown definitions, skipping duplicates."""
    from src.worker.duty.parser import DutyParseError, parse_duty

    if not duties_dir.is_dir():
        return ()
    duties: list = []
    seen: set[str] = set()
    for duty_file in sorted(duties_dir.glob("*.md")):
        try:
            duty = parse_duty(duty_file.read_text(encoding="utf-8"))
        except (DutyParseError, Exception) as exc:
            logger.warning(
                "[SchedulerRuntime] Failed to load duty '%s': %s",
                duty_file.name,
                exc,
            )
            continue
        if duty.duty_id in seen:
            logger.warning(
                "[SchedulerRuntime] Duplicate duty_id '%s' found in '%s'; skipping duplicate definition",
                duty.duty_id,
                duty_file.name,
            )
            continue
        seen.add(duty.duty_id)
        duties.append(duty)
    return tuple(duties)


def load_unique_goals(goals_dir: Path) -> tuple[tuple[Path, object], ...]:
    """Load canonical goals with their source files, skipping duplicate goal IDs."""
    from src.worker.goal.parser import parse_goal

    if not goals_dir.is_dir():
        return ()
    records: list[tuple[Path, object]] = []
    seen: set[str] = set()
    for goal_file in sorted(goals_dir.glob("*.md")):
        try:
            goal = parse_goal(goal_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "[SchedulerRuntime] Failed to parse goal '%s': %s",
                goal_file.name,
                exc,
            )
            continue
        if goal.goal_id in seen:
            logger.warning(
                "[SchedulerRuntime] Duplicate goal_id '%s' found in '%s'; skipping duplicate definition",
                goal.goal_id,
                goal_file.name,
            )
            continue
        seen.add(goal.goal_id)
        records.append((goal_file, goal))
    return tuple(records)


def prioritize_goal_records(
    goal_records: tuple[tuple[Path, object], ...],
    *,
    current_date: str,
) -> tuple[tuple[Path, object], ...]:
    """Sort goal records using the planner's priority ordering."""
    from src.worker.goal.planner import prioritize_goals

    goal_map = {goal.goal_id: (goal_file, goal) for goal_file, goal in goal_records}
    prioritized = prioritize_goals(
        tuple(goal for _, goal in goal_records),
        current_date=current_date,
    )
    return tuple(goal_map[goal.goal_id] for goal in prioritized if goal.goal_id in goal_map)


async def register_scheduler_workers(
    *,
    apscheduler,
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
    goal_inbox_store: SessionInboxStore | None,
    suggestion_store=None,
    feedback_store=None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Register trigger managers, worker schedulers, and recurring jobs."""
    from src.worker.dead_letter import DeadLetterStore
    from src.worker.duty.duty_executor import DutyExecutor
    from src.worker.duty.trigger_manager import TriggerManager
    from src.worker.scheduler import SchedulerConfig, WorkerScheduler
    from src.worker.trust_gate import compute_trust_gate

    trigger_managers: dict[str, object] = {}
    worker_schedulers: dict[str, object] = {}

    for entry in worker_registry.list_all():
        worker = entry.worker
        worker_id = worker.worker_id
        worker_dir = workspace_root / "tenants" / tenant_id / "workers" / worker_id
        tenant = worker_router._tenant_loader.load(tenant_id)
        trust_gate = compute_trust_gate(worker, tenant)

        duty_executor = DutyExecutor(
            worker_router,
            worker_dir / "duties",
            duty_learning_handler=build_duty_learning_handler(
                worker_dir=worker_dir,
                llm_client=llm_client,
                episode_lock=episode_lock,
                memory_orchestrator=memory_orchestrator,
                openviking_client=openviking_client,
                openviking_scope_prefix=openviking_scope_prefix,
                trust_gate=trust_gate,
            ),
        )
        trigger_manager = TriggerManager(apscheduler, event_bus, duty_executor)

        duties_dir = worker_dir / "duties"
        for duty in load_unique_duties(duties_dir):
            if duty.status == "active":
                await trigger_manager.register_duty(duty, tenant_id, worker_id)

        trigger_managers[worker_id] = trigger_manager

        worker_scheduler = WorkerScheduler(
            config=SchedulerConfig(),
            worker_router=worker_router,
            event_bus=event_bus,
            dead_letter_store=DeadLetterStore(
                redis_client=redis_client,
                fallback_dir=workspace_root,
            ),
        )
        worker_schedulers[worker_id] = worker_scheduler

        await register_goal_health_checks(
            scheduler=apscheduler,
            goals_dir=worker_dir / "goals",
            tenant_id=tenant_id,
            worker_id=worker_id,
            worker_scheduler=worker_scheduler,
            event_bus=event_bus,
            inbox_store=goal_inbox_store,
            workspace_root=workspace_root,
        )

        register_worker_recurring_jobs(
            scheduler=apscheduler,
            tenant_id=tenant_id,
            worker_id=worker_id,
            worker_dir=worker_dir,
            workspace_root=workspace_root,
            mcp_server=mcp_server,
            llm_client=llm_client,
            goal_inbox_store=goal_inbox_store,
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
            cross_worker_sharing_enabled=trust_gate.cross_worker_sharing_enabled,
        )

        if mcp_server is not None:
            from src.tools.mcp.tool import Tool
            from src.tools.mcp.types import MCPCategory, RiskLevel, ToolType

            shared_rules_dir = workspace_root / "tenants" / tenant_id / "shared_rules"
            worker_rules_dir = worker_dir / "rules"
            mcp_server.register_tool(
                Tool(
                    name="adopt_shared_rule",
                    description="采纳一条来自其他 Worker 的共享规则",
                    handler=build_adopt_handler(
                        worker_id,
                        worker_rules_dir,
                        shared_rules_dir,
                    ),
                    parameters={
                        "rule_id": {
                            "type": "string",
                            "description": "要采纳的共享规则 ID",
                        }
                    },
                    required_params=("rule_id",),
                    tool_type=ToolType.WRITE,
                    category=MCPCategory.SPECIALIZED,
                    risk_level=RiskLevel.LOW,
                    tags=frozenset({"learning", "shared_rules"}),
                    enabled=True,
                )
            )

    return trigger_managers, worker_schedulers


def register_worker_recurring_jobs(
    *,
    scheduler,
    tenant_id: str,
    worker_id: str,
    worker_dir: Path,
    workspace_root: Path,
    mcp_server,
    llm_client,
    goal_inbox_store: SessionInboxStore | None,
    suggestion_store=None,
    feedback_store=None,
    cross_worker_sharing_enabled: bool = False,
) -> None:
    """Register recurring non-goal scheduler jobs for one worker."""
    from apscheduler.triggers.cron import CronTrigger

    job_ids = build_worker_recurring_job_ids(worker_id)
    scheduler.add_job(
        run_profile_update,
        trigger=CronTrigger.from_crontab("0 2 * * *"),
        id=job_ids["profile_update"],
        args=(worker_id, worker_dir),
        replace_existing=True,
    )
    scheduler.add_job(
        run_crystallization_cycle,
        trigger=CronTrigger.from_crontab("15 2 * * *"),
        id=job_ids["crystallization"],
        args=(worker_dir, mcp_server, llm_client, suggestion_store, tenant_id, worker_id),
        replace_existing=True,
    )
    if suggestion_store is not None:
        scheduler.add_job(
            run_task_pattern_detection,
            trigger=CronTrigger.from_crontab("45 2 * * *"),
            id=job_ids["task_pattern"],
            args=(tenant_id, worker_id, workspace_root, suggestion_store),
            replace_existing=True,
        )
        scheduler.add_job(
            run_goal_completion_advisor,
            trigger=CronTrigger.from_crontab("15 3 * * *"),
            id=job_ids["goal_completion"],
            args=(tenant_id, worker_id, workspace_root, suggestion_store, llm_client),
            replace_existing=True,
        )
        scheduler.add_job(
            run_duty_skill_detection,
            trigger=CronTrigger.from_crontab("30 3 * * *"),
            id=job_ids["duty_to_skill"],
            args=(tenant_id, worker_id, workspace_root, suggestion_store),
            replace_existing=True,
        )
    if suggestion_store is not None and feedback_store is not None:
        scheduler.add_job(
            run_duty_drift_detection,
            trigger=CronTrigger.from_crontab("0 3 * * *"),
            id=job_ids["duty_drift"],
            args=(tenant_id, worker_id, workspace_root, suggestion_store, feedback_store),
            replace_existing=True,
        )
    if cross_worker_sharing_enabled:
        scheduler.add_job(
            run_sharing_cycle,
            trigger=CronTrigger.from_crontab("30 2 * * *"),
            id=job_ids["sharing_cycle"],
            args=(tenant_id, worker_id, worker_dir, goal_inbox_store),
            replace_existing=True,
        )


async def register_goal_health_checks(
    *,
    scheduler,
    goals_dir: Path,
    tenant_id: str,
    worker_id: str,
    worker_scheduler,
    event_bus,
    inbox_store: SessionInboxStore | None = None,
    workspace_root: Path | None = None,
) -> None:
    """Register periodic goal health checks for active goals."""
    goal_records = load_unique_goals(goals_dir)
    active_records = tuple(
        item for item in goal_records
        if getattr(item[1], "status", "") == "active"
    )
    if active_records:
        from src.worker.goal.planner import detect_resource_conflicts

        prioritized_records = prioritize_goal_records(
            active_records,
            current_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )
        conflicts = detect_resource_conflicts(
            tuple(goal for _, goal in prioritized_records)
        )
        if conflicts:
            logger.warning(
                "[SchedulerRuntime] Goal resource conflicts detected: %s",
                ", ".join(f"{left}<->{right}" for left, right in conflicts),
            )
    else:
        prioritized_records = ()

    for goal_file, goal in prioritized_records:
        if goal.status != "active":
            continue

        register_single_goal_health_check(
            scheduler=scheduler,
            goal=goal,
            goal_file=goal_file,
            tenant_id=tenant_id,
            worker_id=worker_id,
            worker_scheduler=worker_scheduler,
            event_bus=event_bus,
            inbox_store=inbox_store,
            workspace_root=workspace_root,
        )


def resolve_goal_file(
    *,
    goal_file: Path,
    tenant_id: str,
    worker_id: str,
    goal_id: str = "",
    workspace_root: Path | None = None,
) -> Path | None:
    """Resolve the current goal markdown path, recovering from stale job args."""
    if goal_file.is_file():
        return goal_file
    if not goal_id or workspace_root is None:
        return None

    from src.worker.integrations.goal_generator import find_goal_file

    return find_goal_file(
        workspace_root / "tenants" / tenant_id / "workers" / worker_id / "goals",
        goal_id,
    )


async def run_goal_health_check(
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
    from src.worker.goal.parser import parse_goal
    from src.worker.lifecycle.detectors import resolve_gate_level
    from src.worker.lifecycle.task_confirmation import (
        confirmation_reason_for,
        enqueue_task_confirmation,
    )
    from src.worker.task import TaskProvenance, create_task_manifest
    from src.worker.goal.progress_checker import check_goal_progress
    from src.worker.scheduler import TriggerPriority

    resolved_goal_file = resolve_goal_file(
        goal_file=goal_file,
        tenant_id=tenant_id,
        worker_id=worker_id,
        goal_id=goal_id,
        workspace_root=workspace_root,
    )
    if resolved_goal_file is None:
        logger.warning(
            "[SchedulerRuntime] Goal file missing for health check: %s",
            goal_file,
        )
        return

    try:
        goal = parse_goal(resolved_goal_file.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(
            "[SchedulerRuntime] Failed to refresh goal '%s': %s",
            resolved_goal_file.name,
            exc,
        )
        return

    if goal.status != "active":
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = check_goal_progress(goal, today)
    if result.recommended_action == "proceed":
        return

    prompt = build_goal_check_prompt(goal, result)
    if inbox_store is not None:
        await inbox_store.write(
            InboxItem(
                tenant_id=tenant_id,
                worker_id=worker_id,
                target_session_key=f"main:{worker_id}",
                source_type="goal_check",
                event_type="goal.health_check_detected",
                priority_hint=25,
                dedupe_key=f"goal_check:{goal.goal_id}:{result.recommended_action}",
                payload={
                    "goal_id": goal.goal_id,
                    "goal_title": goal.title,
                    "goal_file": str(resolved_goal_file),
                    "recommended_action": result.recommended_action,
                    "deviation_score": round(result.deviation_score, 4),
                    "task_description": prompt,
                    "preferred_skill_ids": list(goal.preferred_skill_ids),
                    "pre_script": serialize_pre_script(goal.default_pre_script),
                },
            )
        )
    else:
        provenance = TaskProvenance(
            source_type="goal_followup",
            source_id=goal.goal_id,
            goal_id=goal.goal_id,
        )
        manifest = create_task_manifest(
            worker_id=worker_id,
            tenant_id=tenant_id,
            preferred_skill_ids=goal.preferred_skill_ids,
            provenance=provenance,
            gate_level=resolve_gate_level(
                provenance=provenance,
                task_description=prompt,
            ),
            task_description=prompt,
            pre_script=goal.default_pre_script,
            main_session_key=f"main:{worker_id}",
        )
        if manifest.gate_level == "gated":
            confirmation_store = inbox_store
            if confirmation_store is None and workspace_root is not None:
                confirmation_store = SessionInboxStore(
                    redis_client=None,
                    fallback_dir=workspace_root,
                    event_bus=event_bus,
                )
            if confirmation_store is None:
                logger.info(
                    "[SchedulerRuntime] Goal follow-up task gated for worker '%s' goal '%s'",
                    worker_id,
                    goal.goal_id,
                )
                return
            await enqueue_task_confirmation(
                inbox_store=confirmation_store,
                manifest=manifest,
                task_description=prompt,
                preferred_skill_ids=goal.preferred_skill_ids,
                target_session_key=f"main:{worker_id}",
                reason=confirmation_reason_for(prompt),
                priority_hint=25,
                task_kind="goal_followup",
            )
            return
        accepted = await worker_scheduler.submit_task(
            {
                "task": prompt,
                "tenant_id": tenant_id,
                "worker_id": worker_id,
                "manifest": manifest,
                "thread_id": f"main:{worker_id}",
                "main_session_key": f"main:{worker_id}",
                "preferred_skill_ids": goal.preferred_skill_ids,
            },
            priority=TriggerPriority().GOAL,
        )
        if not accepted and event_bus is not None:
            await event_bus.publish(Event(
                event_id=f"evt-{uuid4().hex[:8]}",
                type="task.failed",
                source="scheduler_runtime",
                tenant_id=tenant_id,
                payload=(
                    ("session_id", ""),
                    ("thread_id", f"main:{worker_id}"),
                    ("task_id", manifest.task_id),
                    ("error_message", "Scheduler quota exhausted"),
                    ("error_code", "QUOTA_EXHAUSTED"),
                    ("worker_id", worker_id),
                ),
            ))

    if (
        event_bus is not None
        and goal.external_source is not None
        and goal.external_source.stakeholders
    ):
        await publish_progress_update_request(
            event_bus=event_bus,
            goal=goal,
            goal_file=resolved_goal_file,
            tenant_id=tenant_id,
            worker_id=worker_id,
            recommended_action=result.recommended_action,
            deviation_score=result.deviation_score,
        )


def build_goal_health_job_id(worker_id: str, goal_id: str) -> str:
    """Build a stable APScheduler job id for one worker goal health check."""
    return f"goal:{worker_id}:{goal_id}:health_check"


def register_single_goal_health_check(
    *,
    scheduler,
    goal,
    goal_file: Path,
    tenant_id: str,
    worker_id: str,
    worker_scheduler,
    event_bus,
    inbox_store: SessionInboxStore | None = None,
    workspace_root: Path | None = None,
) -> None:
    """Register one active goal health check job."""
    from apscheduler.triggers.interval import IntervalTrigger

    interval_seconds = parse_interval_seconds(
        goal.external_source.sync_schedule
        if goal.external_source and goal.external_source.sync_schedule
        else "1h"
    )
    scheduler.add_job(
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


def parse_interval_seconds(interval: str) -> int:
    """Parse an interval string like 15m/1h to seconds."""
    clean = (interval or "1h").strip().lower()
    if clean.endswith("h"):
        return int(clean[:-1]) * 3600
    if clean.endswith("m"):
        return int(clean[:-1]) * 60
    if clean.endswith("s"):
        return int(clean[:-1])
    return int(clean)


def build_goal_check_prompt(goal, result) -> str:
    """Build a worker task prompt from a goal health check result."""
    return "\n".join((
        f"[Goal Health Check] {goal.title}",
        "",
        f"Goal ID: {goal.goal_id}",
        f"Priority: {goal.priority}",
        f"Recommended Action: {result.recommended_action}",
        f"Deviation Score: {result.deviation_score:.2f}",
        f"Overdue Milestones: {', '.join(result.overdue_milestones) or 'none'}",
        f"Stalled Tasks: {', '.join(result.stalled_tasks) or 'none'}",
        f"Newly Actionable Tasks: {', '.join(result.newly_actionable_tasks) or 'none'}",
        "",
        "Please assess the current risk, propose the next best action, "
        "and identify whether a stakeholder progress update is required.",
    ))


async def publish_progress_update_request(
    *,
    event_bus,
    goal,
    goal_file: Path,
    tenant_id: str,
    worker_id: str,
    recommended_action: str,
    deviation_score: float,
) -> None:
    """Publish a follow-up event for stakeholder progress collection."""
    from uuid import uuid4

    from src.events.models import Event

    await event_bus.publish(Event(
        event_id=f"evt-{uuid4().hex[:8]}",
        type="goal.progress_update_requested",
        source="scheduler.goal_health_check",
        tenant_id=tenant_id,
        payload=(
            ("goal_id", goal.goal_id),
            ("goal_title", goal.title),
            ("goal_file", str(goal_file)),
            ("worker_id", worker_id),
            ("recommended_action", recommended_action),
            ("deviation_score", round(deviation_score, 4)),
        ),
    ))


def build_duty_learning_handler(
    worker_dir: Path,
    llm_client,
    episode_lock,
    memory_orchestrator=None,
    openviking_client=None,
    openviking_scope_prefix: str = "viking://",
    trust_gate=None,
):
    """Build a closure for duty post-execution learning."""
    from src.worker.duty.duty_learning import handle_duty_post_execution

    async def _handler(record, duty):
        if episode_lock is None:
            return
        await handle_duty_post_execution(
            record=record,
            duty=duty,
            worker_dir=worker_dir,
            llm_client=llm_client,
            episode_lock=episode_lock,
            memory_orchestrator=memory_orchestrator,
            openviking_client=openviking_client,
            openviking_scope_prefix=openviking_scope_prefix,
            trust_gate=trust_gate,
        )

    return _handler


def build_adopt_handler(
    worker_id: str,
    worker_rules_dir: Path,
    shared_rules_dir: Path,
):
    """Build a closure-backed shared rule adoption handler."""
    from src.worker.rules.shared_store import adopt_shared_rule, load_shared_rules

    def _handler(rule_id: str) -> dict:
        shared_rules = load_shared_rules(shared_rules_dir)
        target = next((item for item in shared_rules if item.rule.rule_id == rule_id), None)
        if target is None:
            return {"success": False, "error": "Shared rule not found"}
        adopted = adopt_shared_rule(target, worker_rules_dir)
        return {
            "success": True,
            "worker_id": worker_id,
            "adopted_rule_id": adopted.rule_id,
        }

    return _handler


def load_duty_records(duties_dir: Path) -> tuple:
    """Load recent duty records across all duties for one worker."""
    from src.worker.duty.execution_log import load_recent_records

    if not duties_dir.exists():
        return ()
    records: list = []
    for duty in load_unique_duties(duties_dir):
        records.extend(load_recent_records(duties_dir / duty.duty_id, limit=50))
    return tuple(records)


def run_task_pattern_detection(
    tenant_id: str,
    worker_id: str,
    workspace_root: Path,
    suggestion_store,
) -> tuple:
    """Cron entrypoint: detect repeated manual tasks."""
    from src.worker.lifecycle.detectors import RepeatedTaskDetector
    from src.worker.task import TaskStore

    detector = RepeatedTaskDetector(
        task_store=TaskStore(workspace_root),
        suggestion_store=suggestion_store,
    )
    return detector.detect(tenant_id=tenant_id, worker_id=worker_id)


def run_duty_drift_detection(
    tenant_id: str,
    worker_id: str,
    workspace_root: Path,
    suggestion_store,
    feedback_store,
) -> tuple:
    """Cron entrypoint: detect drifting duties."""
    from src.worker.lifecycle.detectors import DutyDriftDetector

    detector = DutyDriftDetector(
        suggestion_store=suggestion_store,
        feedback_store=feedback_store,
    )
    worker_dir = workspace_root / "tenants" / tenant_id / "workers" / worker_id
    return detector.detect(
        tenant_id=tenant_id,
        worker_id=worker_id,
        duties_dir=worker_dir / "duties",
    )


async def run_goal_completion_advisor(
    tenant_id: str,
    worker_id: str,
    workspace_root: Path,
    suggestion_store,
    llm_client,
) -> tuple:
    """Cron entrypoint: detect completed goals that should become duties."""
    from src.worker.lifecycle.detectors import GoalCompletionAdvisor

    detector = GoalCompletionAdvisor(
        suggestion_store=suggestion_store,
        llm_client=llm_client,
    )
    worker_dir = workspace_root / "tenants" / tenant_id / "workers" / worker_id
    return await detector.detect(
        tenant_id=tenant_id,
        worker_id=worker_id,
        goals_dir=worker_dir / "goals",
    )


def run_profile_update(worker_id: str, worker_dir: Path) -> None:
    """Cron entrypoint: recompute one worker behavior profile."""
    from dataclasses import replace

    from src.memory.episodic.store import load_index
    from src.worker.profile.updater import (
        compute_behavior_profile,
        detect_behavioral_trends,
        load_profile,
        write_profile,
    )
    from src.worker.rules.rule_manager import load_rules

    memory_dir = worker_dir / "memory"
    rules_dir = worker_dir / "rules"
    previous = load_profile(worker_dir)
    profile = compute_behavior_profile(
        worker_id=worker_id,
        episodes=load_index(memory_dir),
        rules=load_rules(rules_dir),
        duty_records=load_duty_records(worker_dir / "duties"),
        current_date=datetime.now(timezone.utc).isoformat(),
    )
    write_profile(
        worker_dir,
        replace(
            profile,
            behavioral_trends=detect_behavioral_trends(profile, previous),
        ),
    )


async def run_crystallization_cycle(
    worker_dir: Path,
    mcp_server,
    llm_client,
    suggestion_store=None,
    tenant_id: str = "",
    worker_id: str = "",
) -> None:
    """Cron entrypoint: crystallize eligible rules."""
    from src.worker.rules.crystallizer import run_crystallization_cycle as _run

    await _run(
        rules_dir=worker_dir / "rules",
        skills_dir=worker_dir / "skills",
        mcp_server=mcp_server,
        llm_client=llm_client,
        suggestion_store=suggestion_store,
        tenant_id=tenant_id,
        worker_id=worker_id,
    )


async def run_sharing_cycle(
    tenant_id: str,
    worker_id: str,
    worker_dir: Path,
    inbox_store: SessionInboxStore | None,
) -> None:
    """Cron entrypoint: publish adoptable shared rules to inbox."""
    from src.worker.rules.shared_store import run_sharing_cycle as _run

    if inbox_store is None:
        return
    shared_rules = _run(
        worker_id=worker_id,
        worker_rules_dir=worker_dir / "rules",
        shared_rules_dir=worker_dir.parent.parent / "shared_rules",
    )
    for shared_rule in shared_rules:
        await inbox_store.write(
            InboxItem(
                tenant_id=tenant_id,
                worker_id=worker_id,
                target_session_key=f"main:{worker_id}",
                source_type="rule_sharing",
                event_type="learning.shared_rule_available",
                priority_hint=40,
                dedupe_key=f"shared_rule:{shared_rule.rule.rule_id}:{worker_id}",
                payload={
                    "rule_id": shared_rule.rule.rule_id,
                    "shared_by": shared_rule.shared_by,
                    "task_description": (
                        f"Worker {shared_rule.shared_by} 共享了规则："
                        f"{shared_rule.rule.rule}"
                    ),
                },
            )
        )
