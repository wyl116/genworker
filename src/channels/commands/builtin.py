"""Built-in channel commands."""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from uuid import uuid4

from src.channels.models import ReplyContent
from src.common.logger import get_logger
from src.worker.lifecycle.detectors import resolve_gate_level
from src.worker.lifecycle.task_confirmation import CONFIRMATION_EVENT_TYPE
from src.worker.lifecycle.task_confirmation import enqueue_task_confirmation
from src.worker.lifecycle.models import FeedbackRecord
from src.worker.task import TaskProvenance, create_task_manifest

from .models import CommandSpec
from .registry import CommandRegistry
from .approval_events import approval_event_types

logger = get_logger()


def build_builtin_command_registry() -> CommandRegistry:
    registry = CommandRegistry()
    registry.register(
        CommandSpec(
            name="help",
            description="List available commands",
            handler=_help_command,
            aliases=frozenset({"commands"}),
        )
    )
    registry.register(
        CommandSpec(
            name="status",
            description="Show current worker/session status",
            handler=_status_command,
        )
    )
    registry.register(
        CommandSpec(
            name="reset",
            description="Reset the current conversation session",
            handler=_reset_command,
            require_mention=True,
        )
    )
    registry.register(
        CommandSpec(
            name="suggestions",
            description="List pending lifecycle suggestions",
            handler=_suggestions_command,
        )
    )
    registry.register(
        CommandSpec(
            name="goals",
            description="List worker goals, optionally filtered by status",
            handler=_goals_command,
        )
    )
    registry.register(
        CommandSpec(
            name="approve_goal",
            description="Approve a pending goal and activate it",
            handler=_approve_goal_command,
        )
    )
    registry.register(
        CommandSpec(
            name="reject_goal",
            description="Reject a pending goal",
            handler=_reject_goal_command,
        )
    )
    registry.register(
        CommandSpec(
            name="approve_suggestion",
            description="Approve a pending lifecycle suggestion",
            handler=_approve_suggestion_command,
        )
    )
    registry.register(
        CommandSpec(
            name="reject_suggestion",
            description="Reject a pending lifecycle suggestion",
            handler=_reject_suggestion_command,
        )
    )
    registry.register(
        CommandSpec(
            name="preview_suggestion",
            description="Create a gated preview task for one pending suggestion",
            handler=_preview_suggestion_command,
        )
    )
    registry.register(
        CommandSpec(
            name="feedback",
            description="Record structured feedback for a task or duty",
            handler=_feedback_command,
        )
    )
    registry.register(
        CommandSpec(
            name="confirmations",
            description="List pending gated task confirmations",
            handler=_confirmations_command,
        )
    )
    registry.register(
        CommandSpec(
            name="approve_confirmation",
            description="Approve one gated derived task",
            handler=_approve_confirmation_command,
        )
    )
    registry.register(
        CommandSpec(
            name="reject_confirmation",
            description="Reject one gated derived task",
            handler=_reject_confirmation_command,
        )
    )
    return registry


async def _help_command(ctx) -> ReplyContent:
    registry = ctx.registry
    if registry is None:
        return ReplyContent(text="No commands are registered.")
    visible = registry.list_visible(
        channel_type=ctx.message.channel_type,
        trust_level=ctx.tenant.trust_level.name,
    )
    if not visible:
        return ReplyContent(text="No commands are available in this channel.")
    lines = ["Available commands:"]
    for spec in visible:
        lines.append(f"/{spec.name} - {spec.description}")
    return ReplyContent(text="\n".join(lines))


async def _status_command(ctx) -> ReplyContent:
    session = await ctx.session_manager.find_by_thread(ctx.thread_id)
    message_count = len(getattr(session, "messages", ())) if session is not None else 0
    status = [
        f"tenant={ctx.binding.tenant_id}",
        f"worker={ctx.binding.worker_id}",
        f"thread={ctx.thread_id}",
        f"messages={message_count}",
    ]
    return ReplyContent(text="\n".join(status))


async def _reset_command(ctx) -> ReplyContent:
    reset = getattr(ctx.session_manager, "reset_thread", None)
    if callable(reset):
        await reset(ctx.thread_id)
    return ReplyContent(text="当前会话已重置。")


async def _suggestions_command(ctx) -> ReplyContent:
    store = ctx.suggestion_store
    if store is None:
        return ReplyContent(text="Suggestion store is not available.")
    store.expire_pending(ctx.binding.tenant_id, ctx.binding.worker_id)
    pending = store.list_pending(ctx.binding.tenant_id, ctx.binding.worker_id)
    if not pending:
        return ReplyContent(text="当前没有 pending suggestions。")
    lines = ["Pending suggestions:"]
    for record in pending:
        lines.append(f"{record.suggestion_id} | {record.type} | {record.title}")
    return ReplyContent(text="\n".join(lines))


async def _goals_command(ctx) -> ReplyContent:
    argv = tuple(ctx.args.get("argv", ()))
    status_filter = str(argv[0]).strip().lower() if argv else "pending"
    worker_dir = _worker_dir(ctx)
    goals_dir = worker_dir / "goals"
    if not goals_dir.is_dir():
        return ReplyContent(text="当前 worker 没有 goals 目录。")

    from src.runtime.scheduler_runtime import load_unique_goals

    records = load_unique_goals(goals_dir)
    if status_filter != "all":
        wanted_statuses = {
            "pending": {"pending_approval"},
            "pending_approval": {"pending_approval"},
            "active": {"active"},
            "completed": {"completed"},
            "paused": {"paused"},
            "abandoned": {"abandoned"},
        }.get(status_filter)
        if wanted_statuses is None:
            return ReplyContent(text="Usage: /goals [pending|active|completed|paused|abandoned|all]")
        records = tuple(
            item for item in records
            if getattr(item[1], "status", "") in wanted_statuses
        )
    if not records:
        return ReplyContent(text="当前没有匹配的 goals。")

    lines = ["Goals:"]
    for _, goal in records:
        lines.append(f"{goal.goal_id} | {goal.status} | {goal.priority} | {goal.title}")
    return ReplyContent(text="\n".join(lines))


async def _approve_goal_command(ctx) -> ReplyContent:
    argv = tuple(ctx.args.get("argv", ()))
    if not argv:
        return ReplyContent(text="Usage: /approve_goal <goal_id>")
    goal_id = str(argv[0]).strip()
    goal, goal_file = _load_goal_for_update(ctx, goal_id)
    if goal is None or goal_file is None:
        return ReplyContent(text=f"Goal '{goal_id}' 未找到。")

    from dataclasses import replace
    from src.worker.goal.planner import approve_goal
    from src.worker.integrations.goal_generator import write_goal_md

    try:
        approved = approve_goal(goal)
    except ValueError as exc:
        return ReplyContent(text=str(exc))

    approved = replace(approved, approved_by=_command_actor(ctx))
    write_goal_md(approved, goal_file.parent, filename=goal_file.name)
    await _publish_goal_status_event(
        ctx,
        event_type="goal.approved",
        goal=approved,
        goal_file=goal_file,
    )
    return ReplyContent(text=f"Goal '{approved.goal_id}' 已批准并激活。")


async def _reject_goal_command(ctx) -> ReplyContent:
    argv = tuple(ctx.args.get("argv", ()))
    if not argv:
        return ReplyContent(text="Usage: /reject_goal <goal_id> <reason>")
    goal_id = str(argv[0]).strip()
    reason = " ".join(str(item) for item in argv[1:]).strip() or "rejected"
    goal, goal_file = _load_goal_for_update(ctx, goal_id)
    if goal is None or goal_file is None:
        return ReplyContent(text=f"Goal '{goal_id}' 未找到。")
    if goal.status != "pending_approval":
        return ReplyContent(text=f"Goal '{goal_id}' 当前状态为 {goal.status}，不能拒绝。")

    from dataclasses import replace
    from src.worker.integrations.goal_generator import write_goal_md

    rejected = replace(goal, status="abandoned", approved_by=_command_actor(ctx))
    write_goal_md(rejected, goal_file.parent, filename=goal_file.name)
    await _publish_goal_status_event(
        ctx,
        event_type="goal.rejected",
        goal=rejected,
        goal_file=goal_file,
        reason=reason,
    )
    return ReplyContent(text=f"Goal '{goal_id}' 已拒绝，原因: {reason}")


async def _approve_suggestion_command(ctx) -> ReplyContent:
    store = ctx.suggestion_store
    if store is None:
        return ReplyContent(text="Suggestion store is not available.")
    argv = tuple(ctx.args.get("argv", ()))
    if not argv:
        return ReplyContent(text="Usage: /approve_suggestion <suggestion_id>")
    suggestion_id = str(argv[0]).strip()
    record = store.claim_pending(
        ctx.binding.tenant_id,
        ctx.binding.worker_id,
        suggestion_id,
    )
    if record is None:
        return ReplyContent(text=_suggestion_claim_failure_text(ctx, suggestion_id))

    async with _maintain_suggestion_claim(ctx, suggestion_id, record.claim_token):
        checkpointed = _has_approval_checkpoint(record)
        applied = False
        result_text = ""
        artifact_ref = ""
        if checkpointed:
            applied = True
            result_text = _approval_checkpoint_summary(record, suggestion_id)
            artifact_ref = str(getattr(record, "approval_artifact_ref", "") or "")
        elif record.type in {"goal_to_duty", "task_to_duty"}:
            applied, result_text, artifact_ref = await _materialize_duty_from_suggestion(ctx, record)
        elif record.type == "duty_redefine":
            applied, result_text, artifact_ref = await _apply_duty_redefine_suggestion(ctx, record)
        elif record.type in {"duty_to_skill", "rule_to_skill"}:
            applied, result_text, artifact_ref = await _materialize_skill_from_suggestion(ctx, record)
        else:
            applied = True
            result_text = f"Suggestion '{suggestion_id}' approved."

        if not applied:
            store.release_claim(
                ctx.binding.tenant_id,
                ctx.binding.worker_id,
                suggestion_id,
                claim_token=record.claim_token,
            )
            return ReplyContent(text=result_text)

        if not checkpointed:
            try:
                checkpoint_record = store.mark_approval_applied(
                    ctx.binding.tenant_id,
                    ctx.binding.worker_id,
                    suggestion_id,
                    claim_token=record.claim_token,
                    summary=result_text,
                    artifact_ref=artifact_ref,
                )
            except Exception as exc:
                return ReplyContent(text=f"{result_text} 但未能写入 suggestion 审批检查点: {exc}")
            if checkpoint_record is not None:
                record = checkpoint_record

        try:
            resolved = store.resolve(
                ctx.binding.tenant_id,
                ctx.binding.worker_id,
                suggestion_id,
                status="approved",
                resolved_by=_command_actor(ctx),
                resolution_note="approved_via_command",
                claim_token=record.claim_token,
            )
        except Exception as exc:
            store.release_claim(
                ctx.binding.tenant_id,
                ctx.binding.worker_id,
                suggestion_id,
                claim_token=record.claim_token,
            )
            return ReplyContent(text=f"{result_text} 但未能标记 suggestion 已批准: {exc}")
        if resolved is None:
            store.release_claim(
                ctx.binding.tenant_id,
                ctx.binding.worker_id,
                suggestion_id,
                claim_token=record.claim_token,
            )
            return ReplyContent(text=f"{result_text} 但未能标记 suggestion 已批准。")
    _append_feedback(
        ctx,
        FeedbackRecord(
            feedback_id=f"fb-{uuid4().hex[:8]}",
            target_type="suggestion",
            target_id=suggestion_id,
            verdict="approved",
            reason="approved via command",
            created_by=_command_actor(ctx),
        ),
    )
    return ReplyContent(text=result_text)


async def _reject_suggestion_command(ctx) -> ReplyContent:
    store = ctx.suggestion_store
    if store is None:
        return ReplyContent(text="Suggestion store is not available.")
    argv = tuple(ctx.args.get("argv", ()))
    if not argv:
        return ReplyContent(text="Usage: /reject_suggestion <suggestion_id> <reason>")
    suggestion_id = str(argv[0]).strip()
    reason = " ".join(str(item) for item in argv[1:]).strip() or "rejected"
    record = store.claim_pending(
        ctx.binding.tenant_id,
        ctx.binding.worker_id,
        suggestion_id,
    )
    if record is None:
        return ReplyContent(text=_suggestion_claim_failure_text(ctx, suggestion_id))
    if _has_approval_checkpoint(record):
        store.release_claim(
            ctx.binding.tenant_id,
            ctx.binding.worker_id,
            suggestion_id,
            claim_token=record.claim_token,
        )
        return ReplyContent(text=_suggestion_checkpoint_pending_text(record, suggestion_id))
    try:
        resolved = store.resolve(
            ctx.binding.tenant_id,
            ctx.binding.worker_id,
            suggestion_id,
            status="rejected",
            resolved_by=_command_actor(ctx),
            resolution_note=reason,
            claim_token=record.claim_token,
        )
    except Exception:
        store.release_claim(
            ctx.binding.tenant_id,
            ctx.binding.worker_id,
            suggestion_id,
            claim_token=record.claim_token,
        )
        raise
    if resolved is None:
        store.release_claim(
            ctx.binding.tenant_id,
            ctx.binding.worker_id,
            suggestion_id,
            claim_token=record.claim_token,
        )
        return ReplyContent(text=_suggestion_claim_failure_text(ctx, suggestion_id))
    _append_feedback(
        ctx,
        FeedbackRecord(
            feedback_id=f"fb-{uuid4().hex[:8]}",
            target_type="suggestion",
            target_id=suggestion_id,
            verdict="rejected",
            reason=reason,
            created_by=_command_actor(ctx),
        ),
    )
    return ReplyContent(text=f"Suggestion '{suggestion_id}' 已拒绝。")


async def _preview_suggestion_command(ctx) -> ReplyContent:
    store = ctx.suggestion_store
    inbox_store = ctx.inbox_store
    if store is None:
        return ReplyContent(text="Suggestion store is not available.")
    if inbox_store is None:
        return ReplyContent(text="Inbox store is not available.")
    argv = tuple(ctx.args.get("argv", ()))
    if not argv:
        return ReplyContent(text="Usage: /preview_suggestion <suggestion_id>")
    suggestion_id = str(argv[0]).strip()
    record = _get_active_suggestion(ctx, suggestion_id)
    if record is None:
        return ReplyContent(text=_suggestion_claim_failure_text(ctx, suggestion_id))

    task_description, preferred_skill_ids = _build_suggestion_preview_task(record)
    provenance = TaskProvenance(
        source_type="suggestion_preview",
        source_id=record.suggestion_id,
        goal_id=_goal_id_for_suggestion(record),
        duty_id=_duty_id_for_suggestion(record),
        suggestion_id=record.suggestion_id,
    )
    manifest = create_task_manifest(
        worker_id=ctx.binding.worker_id,
        tenant_id=ctx.binding.tenant_id,
        preferred_skill_ids=preferred_skill_ids,
        provenance=provenance,
        gate_level=resolve_gate_level(
            provenance=provenance,
            task_description=task_description,
        ),
        task_description=task_description,
        main_session_key=ctx.thread_id,
    )
    confirmation = await enqueue_task_confirmation(
        inbox_store=inbox_store,
        manifest=manifest,
        task_description=task_description,
        preferred_skill_ids=preferred_skill_ids,
        target_session_key=ctx.thread_id,
        reason=f"Suggestion '{record.suggestion_id}' 的预览任务需要确认后执行。",
        priority_hint=20,
        task_kind="suggestion_preview",
    )
    return ReplyContent(
        text=(
            f"已创建 suggestion preview confirmation: {confirmation.inbox_id}。"
            " 使用 /approve_confirmation 执行预览，或 /reject_confirmation 拒绝。"
        )
    )


async def _feedback_command(ctx) -> ReplyContent:
    argv = tuple(ctx.args.get("argv", ()))
    if len(argv) < 3:
        return ReplyContent(text="Usage: /feedback task|duty <id> approve|reject <reason>")
    target_type = str(argv[0]).strip().lower()
    target_id = str(argv[1]).strip()
    verdict_raw = str(argv[2]).strip().lower()
    reason = " ".join(str(item) for item in argv[3:]).strip()
    if target_type not in {"task", "duty"}:
        return ReplyContent(text="feedback target must be task or duty.")
    verdict_map = {"approve": "approved", "approved": "approved", "reject": "rejected", "rejected": "rejected"}
    verdict = verdict_map.get(verdict_raw)
    if verdict is None:
        return ReplyContent(text="feedback verdict must be approve or reject.")
    _append_feedback(
        ctx,
        FeedbackRecord(
            feedback_id=f"fb-{uuid4().hex[:8]}",
            target_type=target_type,
            target_id=target_id,
            verdict=verdict,
            reason=reason,
            created_by=_command_actor(ctx),
        ),
    )
    return ReplyContent(text=f"已记录 {target_type} '{target_id}' 的 {verdict} 反馈。")


async def _confirmations_command(ctx) -> ReplyContent:
    store = ctx.inbox_store
    if store is None:
        return ReplyContent(text="Inbox store is not available.")
    pending = await store.list_pending(
        tenant_id=ctx.binding.tenant_id,
        worker_id=ctx.binding.worker_id,
        event_type=CONFIRMATION_EVENT_TYPE,
        limit=50,
    )
    if not pending:
        return ReplyContent(text="当前没有 pending confirmations。")
    lines = ["Pending confirmations:"]
    for item in pending:
        payload = dict(item.payload)
        lines.append(
            f"{item.inbox_id} | {payload.get('task_kind', 'task')} | "
            f"{payload.get('reason', '')}"
        )
    return ReplyContent(text="\n".join(lines))


async def _approve_confirmation_command(ctx) -> ReplyContent:
    store = ctx.inbox_store
    scheduler = (ctx.worker_schedulers or {}).get(ctx.binding.worker_id)
    task_store = ctx.task_store
    if store is None:
        return ReplyContent(text="Inbox store is not available.")
    if scheduler is None:
        return ReplyContent(text="Worker scheduler is not available.")
    argv = tuple(ctx.args.get("argv", ()))
    if not argv:
        return ReplyContent(text="Usage: /approve_confirmation <inbox_id>")
    inbox_id = str(argv[0]).strip()
    item = await store.claim_pending(
        inbox_id,
        tenant_id=ctx.binding.tenant_id,
        worker_id=ctx.binding.worker_id,
        event_type="",
    )
    if item is None:
        return ReplyContent(text="Confirmation not found or already handled.")
    if item.event_type not in approval_event_types():
        requeue_warning = await _requeue_confirmation_best_effort(ctx, inbox_id)
        return ReplyContent(text=f"Not an approval item.{requeue_warning}".rstrip())
    if dict(item.payload).get("engine") == "langgraph":
        try:
            accepted = await scheduler.submit_langgraph_resume(
                {
                    "tenant_id": ctx.binding.tenant_id,
                    "worker_id": ctx.binding.worker_id,
                    "thread_id": str(item.payload.get("thread_id", "") or ""),
                    "skill_id": str(item.payload.get("skill_id", "") or ""),
                    "decision": {
                        "approved": True,
                        "note": " ".join(str(part) for part in argv[1:]).strip(),
                    },
                    "expected_digest": str(item.payload.get("state_digest", "") or ""),
                    "inbox_id": item.inbox_id,
                },
                priority=15,
            )
        except Exception as exc:
            requeue_warning = await _requeue_confirmation_best_effort(ctx, item.inbox_id)
            return ReplyContent(text=f"流程提交调度失败: {exc}{requeue_warning}".rstrip())
        if not accepted:
            requeue_warning = await _requeue_confirmation_best_effort(ctx, item.inbox_id)
            return ReplyContent(text=f"流程未进入调度队列，请稍后重试。{requeue_warning}".rstrip())
        return ReplyContent(text=f"已批准 {item.inbox_id}，流程恢复中。")
    if task_store is None:
        requeue_warning = await _requeue_confirmation_best_effort(ctx, item.inbox_id)
        return ReplyContent(text=f"Task store is not available.{requeue_warning}".rstrip())
    manifest = _manifest_from_confirmation(item)
    if manifest is None:
        requeue_warning = await _requeue_confirmation_best_effort(ctx, item.inbox_id)
        return ReplyContent(text=f"Confirmation payload is invalid.{requeue_warning}".rstrip())
    task_store.save(manifest)
    payload = dict(item.payload)
    session = await ctx.session_manager.find_by_thread(ctx.thread_id)
    session_id = getattr(session, "session_id", "") if session is not None else ""
    main_session_key = manifest.main_session_key or ctx.thread_id
    try:
        accepted = await scheduler.submit_task(
            {
                "task": str(payload.get("task_description", "") or manifest.task_description),
                "tenant_id": ctx.binding.tenant_id,
                "worker_id": ctx.binding.worker_id,
                "manifest": manifest,
                "session_id": session_id,
                "thread_id": ctx.thread_id,
                "main_session_key": main_session_key,
                "preferred_skill_ids": tuple(payload.get("preferred_skill_ids", ())),
            },
            priority=15,
        )
    except Exception as exc:
        task_store.save(manifest.mark_error(f"Scheduler submit failed: {exc}"))
        requeue_warning = await _requeue_confirmation_best_effort(ctx, item.inbox_id)
        return ReplyContent(text=f"任务提交调度失败: {exc}{requeue_warning}".rstrip())
    if not accepted:
        task_store.save(manifest.mark_error("Scheduler quota exhausted"))
        requeue_warning = await _requeue_confirmation_best_effort(ctx, item.inbox_id)
        return ReplyContent(text=f"任务未进入调度队列，可能已达到当日配额。{requeue_warning}".rstrip())
    consume_warning = ""
    try:
        await store.mark_consumed(
            [item.inbox_id],
            tenant_id=ctx.binding.tenant_id,
            worker_id=ctx.binding.worker_id,
        )
    except Exception as exc:
        consume_warning = f" 但未能标记 confirmation 已处理: {exc}"
    _append_feedback(
        ctx,
        FeedbackRecord(
            feedback_id=f"fb-{uuid4().hex[:8]}",
            target_type="task",
            target_id=manifest.task_id,
            verdict="approved",
            reason="approved via confirmation command",
            created_by=_command_actor(ctx),
        ),
    )
    return ReplyContent(text=f"已批准任务 '{manifest.task_id}'，并提交执行。{consume_warning}".rstrip())


async def _reject_confirmation_command(ctx) -> ReplyContent:
    store = ctx.inbox_store
    scheduler = (ctx.worker_schedulers or {}).get(ctx.binding.worker_id)
    task_store = ctx.task_store
    if store is None:
        return ReplyContent(text="Inbox store is not available.")
    argv = tuple(ctx.args.get("argv", ()))
    if not argv:
        return ReplyContent(text="Usage: /reject_confirmation <inbox_id> <reason>")
    inbox_id = str(argv[0]).strip()
    item = await store.claim_pending(
        inbox_id,
        tenant_id=ctx.binding.tenant_id,
        worker_id=ctx.binding.worker_id,
        event_type="",
    )
    if item is None:
        return ReplyContent(text="Confirmation not found or already handled.")
    if item.event_type not in approval_event_types():
        requeue_warning = await _requeue_confirmation_best_effort(ctx, inbox_id)
        return ReplyContent(text=f"Not an approval item.{requeue_warning}".rstrip())
    if dict(item.payload).get("engine") == "langgraph":
        if scheduler is None:
            requeue_warning = await _requeue_confirmation_best_effort(ctx, item.inbox_id)
            return ReplyContent(text=f"Worker scheduler is not available.{requeue_warning}".rstrip())
        reason = " ".join(str(part) for part in argv[1:]).strip() or "rejected"
        try:
            accepted = await scheduler.submit_langgraph_resume(
                {
                    "tenant_id": ctx.binding.tenant_id,
                    "worker_id": ctx.binding.worker_id,
                    "thread_id": str(item.payload.get("thread_id", "") or ""),
                    "skill_id": str(item.payload.get("skill_id", "") or ""),
                    "decision": {
                        "approved": False,
                        "note": reason,
                    },
                    "expected_digest": str(item.payload.get("state_digest", "") or ""),
                    "inbox_id": item.inbox_id,
                },
                priority=15,
            )
        except Exception as exc:
            requeue_warning = await _requeue_confirmation_best_effort(ctx, item.inbox_id)
            return ReplyContent(text=f"流程提交调度失败: {exc}{requeue_warning}".rstrip())
        if not accepted:
            requeue_warning = await _requeue_confirmation_best_effort(ctx, item.inbox_id)
            return ReplyContent(text=f"流程未进入调度队列，请稍后重试。{requeue_warning}".rstrip())
        return ReplyContent(text=f"已拒绝 {item.inbox_id}，流程恢复中。")
    manifest = _manifest_from_confirmation(item)
    if manifest is None:
        requeue_warning = await _requeue_confirmation_best_effort(ctx, item.inbox_id)
        return ReplyContent(text=f"Confirmation payload is invalid.{requeue_warning}".rstrip())
    reason = " ".join(str(part) for part in argv[1:]).strip() or "rejected"
    if task_store is not None:
        task_store.save(manifest.mark_error(f"Rejected via confirmation: {reason}"))
    consume_warning = ""
    try:
        await store.mark_consumed(
            [item.inbox_id],
            tenant_id=ctx.binding.tenant_id,
            worker_id=ctx.binding.worker_id,
        )
    except Exception as exc:
        consume_warning = f" 但未能标记 confirmation 已处理: {exc}"
    _append_feedback(
        ctx,
        FeedbackRecord(
            feedback_id=f"fb-{uuid4().hex[:8]}",
            target_type="task",
            target_id=manifest.task_id,
            verdict="rejected",
            reason=reason,
            created_by=_command_actor(ctx),
        ),
    )
    return ReplyContent(text=f"已拒绝任务确认 '{item.inbox_id}'。{consume_warning}".rstrip())


async def _materialize_duty_from_suggestion(ctx, record) -> tuple[bool, str, str]:
    payload = dict(record.payload_dict)
    if not str(payload.get("duty_id", "") or "").strip():
        payload["duty_id"] = _default_duty_id_for_suggestion(record)
    try:
        if getattr(ctx, "lifecycle_services", None) is not None:
            duty, path = ctx.lifecycle_services.materialize_duty_from_payload(
                tenant_id=ctx.binding.tenant_id,
                worker_id=ctx.binding.worker_id,
                payload=payload,
                default_title=record.title,
            )
        else:
            from src.worker.lifecycle.duty_builder import build_duty_from_payload, write_duty_md

            duties_dir = (
                Path(ctx.workspace_root) / "tenants" / ctx.binding.tenant_id
                / "workers" / ctx.binding.worker_id / "duties"
            )
            duty = build_duty_from_payload(payload, default_title=record.title)
            path = write_duty_md(duty, duties_dir)
    except Exception as exc:
        return False, f"应用 suggestion 失败: {exc}", ""
    manager = (ctx.trigger_managers or {}).get(ctx.binding.worker_id)
    if manager is not None:
        try:
            await manager.register_duty(duty, ctx.binding.tenant_id, ctx.binding.worker_id)
        except Exception as exc:
            return True, (
                f"已创建 Duty '{duty.duty_id}' 并写入 {path.name}，"
                f"但未能同步注册触发器: {exc}"
            ), f"duty:{duty.duty_id}"
    return True, f"已创建 Duty '{duty.duty_id}' 并写入 {path.name}。", f"duty:{duty.duty_id}"


async def _apply_duty_redefine_suggestion(ctx, record) -> tuple[bool, str, str]:
    payload = record.payload_dict
    duty_id = str(payload.get("duty_id", "") or record.source_entity_id)
    try:
        if getattr(ctx, "lifecycle_services", None) is not None:
            duty = ctx.lifecycle_services.apply_duty_redefine_payload(
                tenant_id=ctx.binding.tenant_id,
                worker_id=ctx.binding.worker_id,
                duty_id=duty_id,
                payload=payload,
            )
        else:
            from src.worker.lifecycle.duty_builder import apply_duty_redefine

            duties_dir = (
                Path(ctx.workspace_root) / "tenants" / ctx.binding.tenant_id
                / "workers" / ctx.binding.worker_id / "duties"
            )
            duty = apply_duty_redefine(duties_dir, duty_id, payload)
    except Exception as exc:
        return False, f"应用 Duty redefine suggestion 失败: {exc}", ""
    if duty is None:
        return False, f"Duty '{duty_id}' 未找到，无法应用 redefine suggestion。", ""
    manager = (ctx.trigger_managers or {}).get(ctx.binding.worker_id)
    if manager is not None:
        try:
            await manager.unregister_duty(duty.duty_id)
            if duty.status == "active":
                await manager.register_duty(duty, ctx.binding.tenant_id, ctx.binding.worker_id)
        except Exception as exc:
            return True, f"已更新 Duty '{duty.duty_id}'，但未能同步触发器: {exc}", f"duty:{duty.duty_id}"
    return True, f"已更新 Duty '{duty.duty_id}'。", f"duty:{duty.duty_id}"


async def _materialize_skill_from_suggestion(ctx, record) -> tuple[bool, str, str]:
    payload = dict(record.payload_dict)
    try:
        services = getattr(ctx, "lifecycle_services", None)
        if services is None:
            from src.worker.lifecycle.services import LifecycleServices

            services = LifecycleServices(workspace_root=Path(ctx.workspace_root))
        skill, path = await services.materialize_skill_from_payload(
            tenant_id=ctx.binding.tenant_id,
            worker_id=ctx.binding.worker_id,
            payload=payload,
            llm_client=getattr(ctx, "llm_client", None),
            source_record=record,
        )
    except Exception as exc:
        return False, f"应用 Skill suggestion 失败: {exc}", ""
    try:
        refreshed = _refresh_worker_skills_runtime(ctx)
    except Exception as exc:
        return True, (
            f"已创建 Skill '{skill.skill_id}' 并写入 {path.name}，"
            f"但未能立即刷新运行时技能注册表: {exc}"
        ), f"skill:{skill.skill_id}"
    if not refreshed:
        return True, (
            f"已创建 Skill '{skill.skill_id}' 并写入 {path.name}，"
            "运行时技能注册表将在后续自动 reload 后生效。"
        ), f"skill:{skill.skill_id}"
    return True, f"已创建 Skill '{skill.skill_id}' 并写入 {path.name}。", f"skill:{skill.skill_id}"


def _build_suggestion_preview_task(record) -> tuple[str, tuple[str, ...]]:
    payload = record.payload_dict
    preferred_skill_ids = tuple(
        str(item).strip()
        for item in payload.get("preferred_skill_ids", ())
        if str(item).strip()
    )
    details = [f"[Suggestion Preview] {record.title}", ""]
    details.append(f"Suggestion ID: {record.suggestion_id}")
    details.append(f"Type: {record.type}")
    details.append(f"Source Entity: {record.source_entity_type}:{record.source_entity_id}")
    details.append(f"Reason: {record.reason}")
    details.append("")
    if record.type in {"task_to_duty", "goal_to_duty"}:
        details.extend((
            "Please review this proposed duty draft and provide a dry-run preview.",
            "Do not execute external side effects. Focus on expected cadence, inputs, outputs, and risks.",
            "",
            f"Draft Duty Title: {payload.get('title', record.title)}",
            f"Draft Duty ID: {payload.get('duty_id', '')}",
            f"Draft Schedule: {payload.get('schedule', 'n/a')}",
            f"Draft Action: {payload.get('action', '')}",
            f"Quality Criteria: {', '.join(payload.get('quality_criteria', ())) or 'none'}",
        ))
    elif record.type == "duty_redefine":
        details.extend((
            "Please review this duty redefine proposal and summarize the expected impact before applying it.",
            "Do not modify files or trigger external actions.",
            "",
            f"Target Duty ID: {payload.get('duty_id', record.source_entity_id)}",
            f"Recommended Action: {payload.get('recommended_action', '')}",
            f"Suggested Changes: {payload.get('suggested_changes', {})}",
        ))
    elif record.type in {"duty_to_skill", "rule_to_skill"}:
        source_label = "Duty" if record.type == "duty_to_skill" else "Rule"
        details.extend((
            f"请审核此 {source_label} -> Skill 进化提案，评估生成的技能定义是否合理。",
            "不要执行外部操作。关注技能指令的清晰度、关键词覆盖范围和执行策略。",
            "",
            f"Draft Skill ID: {payload.get('skill_id', '')}",
            f"Draft Description: {payload.get('description', '')}",
            f"Keywords: {', '.join(payload.get('keywords', ())) or 'none'}",
            f"Strategy: {payload.get('strategy_mode', 'autonomous')}",
            f"Instructions Seed: {str(payload.get('instructions_seed', '') or '')[:200]}",
        ))
    else:
        details.extend((
            "Please review this lifecycle suggestion and provide a safe dry-run preview.",
            "Do not perform external side effects.",
        ))
    return "\n".join(details), preferred_skill_ids


async def _publish_goal_status_event(
    ctx,
    *,
    event_type: str,
    goal,
    goal_file: Path,
    reason: str = "",
) -> None:
    """Emit goal lifecycle events so downstream runtime subscriptions stay in sync."""
    event_bus = getattr(ctx, "event_bus", None)
    if event_bus is None:
        return

    from src.events.models import Event

    payload = [
        ("goal_id", goal.goal_id),
        ("worker_id", ctx.binding.worker_id),
        ("tenant_id", ctx.binding.tenant_id),
        ("goal_file", str(goal_file)),
        ("status", getattr(goal, "status", "")),
        ("approved_by", getattr(goal, "approved_by", "")),
    ]
    if reason:
        payload.append(("reason", reason))
    await event_bus.publish(Event(
        event_id=f"evt-{uuid4().hex[:8]}",
        type=event_type,
        source="channel_command",
        tenant_id=ctx.binding.tenant_id,
        payload=tuple(payload),
    ))


def _goal_id_for_suggestion(record) -> str:
    if record.type != "goal_to_duty":
        return ""
    payload = record.payload_dict
    return str(payload.get("source_goal_id", "") or record.source_entity_id)


def _duty_id_for_suggestion(record) -> str:
    payload = record.payload_dict
    if record.type == "duty_redefine":
        return str(payload.get("duty_id", "") or record.source_entity_id)
    if record.type == "duty_to_skill":
        return str(payload.get("source_duty_id", "") or record.source_entity_id)
    return ""


def _append_feedback(ctx, record: FeedbackRecord) -> None:
    store = ctx.feedback_store
    if store is None:
        return
    try:
        store.append(ctx.binding.tenant_id, ctx.binding.worker_id, record)
    except Exception as exc:
        logger.warning(
            "[builtin_commands] Failed to append feedback target=%s:%s: %s",
            record.target_type,
            record.target_id,
            exc,
        )


def _suggestion_claim_failure_text(ctx, suggestion_id: str) -> str:
    store = ctx.suggestion_store
    if store is None:
        return f"Suggestion '{suggestion_id}' not found or not pending."
    state = store.get_state(
        ctx.binding.tenant_id,
        ctx.binding.worker_id,
        suggestion_id,
    )
    if state == "approved":
        return f"Suggestion '{suggestion_id}' 已批准。"
    if state == "rejected":
        return f"Suggestion '{suggestion_id}' 已拒绝。"
    if state == "expired":
        return f"Suggestion '{suggestion_id}' 已过期。"
    if state == "claimed":
        return f"Suggestion '{suggestion_id}' 正在处理中。"
    return f"Suggestion '{suggestion_id}' not found or not pending."


def _has_approval_checkpoint(record) -> bool:
    return bool(str(getattr(record, "approval_stage", "") or "").strip())


def _approval_checkpoint_summary(record, suggestion_id: str) -> str:
    summary = str(getattr(record, "approval_summary", "") or "").strip()
    if summary:
        return summary
    artifact_ref = str(getattr(record, "approval_artifact_ref", "") or "").strip()
    if artifact_ref:
        return f"Suggestion '{suggestion_id}' 已执行落地，等待完成批准收口。({artifact_ref})"
    return f"Suggestion '{suggestion_id}' 已执行落地，等待完成批准收口。"


def _suggestion_checkpoint_pending_text(record, suggestion_id: str) -> str:
    summary = _approval_checkpoint_summary(record, suggestion_id)
    return f"{summary} 当前不能拒绝，请重新执行 /approve_suggestion 完成状态收口。"


def _get_active_suggestion(ctx, suggestion_id: str):
    store = ctx.suggestion_store
    if store is None:
        return None
    return store.get_pending_active(
        ctx.binding.tenant_id,
        ctx.binding.worker_id,
        suggestion_id,
    )


async def _requeue_confirmation_best_effort(ctx, inbox_id: str) -> str:
    store = ctx.inbox_store
    if store is None:
        return ""
    try:
        await store.requeue_processing(
            [inbox_id],
            tenant_id=ctx.binding.tenant_id,
            worker_id=ctx.binding.worker_id,
        )
    except Exception as exc:
        return f" 但未能回滚 confirmation 状态: {exc}"
    return ""


@contextlib.asynccontextmanager
async def _maintain_suggestion_claim(ctx, suggestion_id: str, claim_token: str):
    store = ctx.suggestion_store
    if store is None or not claim_token or not hasattr(store, "touch_claim"):
        yield
        return
    heartbeat_task = asyncio.create_task(
        _suggestion_claim_heartbeat_loop(ctx, suggestion_id, claim_token)
    )
    try:
        yield
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task


async def _suggestion_claim_heartbeat_loop(ctx, suggestion_id: str, claim_token: str) -> None:
    store = ctx.suggestion_store
    if store is None:
        return
    interval_seconds = _suggestion_claim_heartbeat_interval_seconds(store)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            touched = store.touch_claim(
                ctx.binding.tenant_id,
                ctx.binding.worker_id,
                suggestion_id,
                claim_token=claim_token,
            )
        except Exception as exc:
            logger.warning(
                "[builtin_commands] Failed to refresh suggestion claim %s: %s",
                suggestion_id,
                exc,
            )
            return
        if not touched:
            return


def _suggestion_claim_heartbeat_interval_seconds(store) -> float:
    interval_fn = getattr(store, "claim_heartbeat_interval_seconds", None)
    if callable(interval_fn):
        try:
            return max(0.05, float(interval_fn()))
        except Exception:
            return 30.0
    timeout_seconds = getattr(store, "_CLAIM_TIMEOUT_SECONDS", 600)
    try:
        return max(0.05, min(30.0, float(timeout_seconds) / 3))
    except Exception:
        return 30.0


async def _get_confirmation_item(ctx, inbox_id: str):
    store = ctx.inbox_store
    if store is None or not inbox_id:
        return None
    item = await store.get_by_id(
        inbox_id,
        tenant_id=ctx.binding.tenant_id,
        worker_id=ctx.binding.worker_id,
    )
    if item is None or item.status != "PENDING" or item.event_type != CONFIRMATION_EVENT_TYPE:
        return None
    return item


def _manifest_from_confirmation(item):
    from src.worker.task import TaskManifest

    payload = dict(item.payload)
    manifest_raw = payload.get("manifest")
    if not isinstance(manifest_raw, dict):
        return None
    try:
        return TaskManifest.from_dict(manifest_raw).mark_pending()
    except Exception:
        return None


def _command_actor(ctx) -> str:
    sender_id = getattr(ctx.message, "sender_id", "") or "unknown"
    return f"user:{sender_id}"


def _default_duty_id_for_suggestion(record) -> str:
    from src.worker.lifecycle.duty_builder import stable_duty_id

    source = str(getattr(record, "source_entity_id", "") or "").strip()
    if source:
        return stable_duty_id(source)
    return stable_duty_id(
        str(getattr(record, "suggestion_id", "") or "lifecycle-duty")
    )


def _worker_dir(ctx) -> Path:
    return (
        Path(ctx.workspace_root) / "tenants" / ctx.binding.tenant_id
        / "workers" / ctx.binding.worker_id
    )


def _refresh_worker_skills_runtime(ctx) -> bool:
    worker_router = getattr(ctx, "worker_router", None)
    workspace_root = getattr(ctx, "workspace_root", None)
    if worker_router is None or workspace_root is None:
        return False

    from src.worker.loader import load_worker_entry
    from src.worker.registry import replace_worker_entry

    existing_registry = getattr(worker_router, "_worker_registry", None)
    if existing_registry is None:
        return False
    worker_entry = load_worker_entry(
        workspace_root=Path(workspace_root),
        tenant_id=ctx.binding.tenant_id,
        worker_id=ctx.binding.worker_id,
    )
    updated_registry = replace_worker_entry(existing_registry, worker_entry)
    if hasattr(worker_router, "replace_worker_registry"):
        worker_router.replace_worker_registry(updated_registry)
    else:
        worker_router._worker_registry = updated_registry
    return True


def _load_goal_for_update(ctx, goal_id: str):
    from src.worker.goal.parser import parse_goal
    from src.worker.integrations.goal_generator import find_goal_file

    worker_dir = _worker_dir(ctx)
    goal_file = find_goal_file(worker_dir / "goals", goal_id)
    if goal_file is None:
        return None, None
    try:
        return parse_goal(goal_file.read_text(encoding="utf-8")), goal_file
    except Exception:
        return None, None
