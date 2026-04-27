"""Heartbeat runner for per-worker cognitive turns."""
from __future__ import annotations

import json
from typing import Any

from src.autonomy.inbox import InboxItem, SessionInboxStore
from src.autonomy.main_session import MainSessionRuntime
from src.common.logger import get_logger
from src.common.time import utc_now_iso
from src.conversation.models import ChatMessage
from src.events.models import Event, EventBusProtocol, Subscription
from src.worker.lifecycle.detectors import resolve_gate_level
from src.worker.lifecycle.task_confirmation import (
    CONFIRMATION_EVENT_TYPE,
    confirmation_reason_for,
    enqueue_task_confirmation,
)
from src.worker.scripts.models import deserialize_pre_script
from src.worker.task import TaskProvenance, TaskStatus, create_task_manifest

from .ledger import AttentionLedger
from .strategy import HeartbeatAction, HeartbeatStrategy

logger = get_logger()


class HeartbeatRunner:
    """Process one worker's inbox and drive the unified execution pipeline."""

    def __init__(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        inbox_store: SessionInboxStore,
        worker_router: Any,
        main_session_runtime: MainSessionRuntime,
        attention_ledger: AttentionLedger,
        worker_scheduler: Any | None = None,
        task_store: Any | None = None,
        isolated_run_manager: Any | None = None,
        strategy: HeartbeatStrategy | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._worker_id = worker_id
        self._inbox_store = inbox_store
        self._worker_router = worker_router
        self._main_session_runtime = main_session_runtime
        self._attention_ledger = attention_ledger
        self._worker_scheduler = worker_scheduler
        self._task_store = task_store
        self._isolated_run_manager = isolated_run_manager
        self._strategy = strategy or HeartbeatStrategy()
        self._event_bus: EventBusProtocol | None = None
        self._subscription_ids: list[str] = []

    async def start(self, event_bus: EventBusProtocol) -> str:
        """Subscribe to inbox.item_written for observability and future wakeups."""
        self._event_bus = event_bus
        subscription = Subscription(
            handler_id=f"heartbeat:{self._worker_id}:inbox_written",
            event_type="inbox.item_written",
            tenant_id=self._tenant_id,
            handler=self._on_inbox_written,
        )
        handler_id = event_bus.subscribe(subscription)
        self._subscription_ids.append(handler_id)
        return handler_id

    async def stop(self) -> None:
        """Cancel EventBus subscriptions."""
        if self._event_bus is not None:
            for handler_id in self._subscription_ids:
                self._event_bus.unsubscribe(self._tenant_id, handler_id)
        self._subscription_ids.clear()

    def update_strategy(self, strategy: HeartbeatStrategy) -> None:
        """Swap strategy at runtime for hot-reload scenarios."""
        self._strategy = strategy

    def replace_worker_router(self, worker_router: Any) -> None:
        """Refresh the worker router used for heartbeat cognition turns."""
        self._worker_router = worker_router

    def replace_runtime_dependencies(
        self,
        *,
        worker_scheduler: Any | None = None,
        isolated_run_manager: Any | None = None,
    ) -> None:
        """Refresh mutable scheduler bindings after runtime reload."""
        self._worker_scheduler = worker_scheduler
        self._isolated_run_manager = isolated_run_manager

    @property
    def strategy(self) -> HeartbeatStrategy:
        """Expose current strategy for diagnostics and refresh flows."""
        return self._strategy

    async def run_once(
        self,
        tenant_id: str | None = None,
        worker_id: str | None = None,
    ) -> None:
        """Run one heartbeat turn for a single worker."""
        tenant_id = tenant_id or self._tenant_id
        worker_id = worker_id or self._worker_id

        pending = await self._inbox_store.fetch_pending(
            tenant_id=tenant_id,
            worker_id=worker_id,
            exclude_event_types=(CONFIRMATION_EVENT_TYPE,),
        )
        if not pending:
            await self._main_session_runtime.update_heartbeat_state()
            return

        inbox_ids = [item.inbox_id for item in pending]
        task_refs: list[str] = []
        concerns: list[str] = []
        try:
            actionable, suppressed = await self._split_actionable_items(pending)
            if actionable:
                item_actions = {
                    item.inbox_id: self._strategy.decide_action(item)
                    for item in actionable
                }
                task_refs.extend(
                    await self._dispatch_task_items(actionable, item_actions)
                )
                summary_items = [
                    item for item in actionable
                    if item_actions[item.inbox_id].kind == "summary"
                ]
                if summary_items:
                    await self._run_cognitive_turn(summary_items)
                for item in actionable:
                    concern = self._concern_key(item)
                    if concern:
                        concerns.append(concern)
            await self._mark_notifications(actionable)
            await self._inbox_store.mark_consumed(
                inbox_ids,
                tenant_id=tenant_id,
                worker_id=worker_id,
            )
            await self._main_session_runtime.update_heartbeat_state(
                inbox_cursor=max((item.created_at for item in pending), default=None),
                open_concerns=tuple(dict.fromkeys(concerns)),
                task_refs=tuple(dict.fromkeys(task_refs)),
                last_heartbeat_at=utc_now_iso(),
            )
            if suppressed:
                logger.debug(
                    "[HeartbeatRunner] Suppressed %s deduped inbox items for %s",
                    len(suppressed),
                    worker_id,
                )
        except Exception as exc:
            logger.error(
                "[HeartbeatRunner] Heartbeat failed for worker %s: %s",
                worker_id,
                exc,
                exc_info=True,
            )
            await self._inbox_store.requeue_processing(
                inbox_ids,
                tenant_id=tenant_id,
                worker_id=worker_id,
            )
            await self._main_session_runtime.append_message(
                ChatMessage(
                    role="system",
                    content=f"[Heartbeat 失败] {exc}",
                )
            )

    async def _run_cognitive_turn(self, items: list[InboxItem]) -> None:
        prompt = self._build_digest(items)
        task_context = await self._main_session_runtime.build_task_context()
        content_parts: list[str] = []
        async for event in self._worker_router.route_stream(
            task=prompt,
            tenant_id=self._tenant_id,
            worker_id=self._worker_id,
            task_context=task_context,
        ):
            content = getattr(event, "content", "")
            if content:
                content_parts.append(content)
            if getattr(event, "event_type", "") == "ERROR":
                raise RuntimeError(getattr(event, "message", "heartbeat route failed"))
        summary = "".join(content_parts).strip()
        if summary:
            await self._main_session_runtime.append_message(
                ChatMessage(role="assistant", content=summary)
            )

    async def _dispatch_task_items(
        self,
        items: list[InboxItem],
        item_actions: dict[str, HeartbeatAction],
    ) -> list[str]:
        task_refs: list[str] = []
        main_session = None
        for item in items:
            action = item_actions.get(item.inbox_id, HeartbeatAction("summary"))
            if action.kind == "summary" or not action.task_description:
                continue
            provenance = self._build_task_provenance(item)
            pre_script = deserialize_pre_script(item.payload.get("pre_script"))
            gate_level = resolve_gate_level(
                provenance=provenance,
                task_description=action.task_description,
            )
            if gate_level == "gated":
                manifest = create_task_manifest(
                    worker_id=self._worker_id,
                    tenant_id=self._tenant_id,
                    preferred_skill_ids=action.preferred_skill_ids,
                    provenance=provenance,
                    gate_level=gate_level,
                    task_description=action.task_description,
                    pre_script=pre_script,
                    main_session_key=self._main_session_runtime.session_key,
                )
                await enqueue_task_confirmation(
                    inbox_store=self._inbox_store,
                    manifest=manifest,
                    task_description=action.task_description,
                    preferred_skill_ids=action.preferred_skill_ids,
                    target_session_key=self._main_session_runtime.session_key,
                    reason=confirmation_reason_for(action.task_description),
                    priority_hint=item.priority_hint,
                    task_kind=action.kind,
                )
                await self._main_session_runtime.append_message(
                    ChatMessage(
                        role="system",
                        content=(
                            "[Heartbeat 拦截] 派生任务需要人工确认后才能执行。"
                            " 可使用 /confirmations 查看，"
                            f"任务内容: {action.task_description}"
                        ),
                    )
                )
                continue
            if action.kind == "isolated" and self._isolated_run_manager is not None:
                manifest = await self._isolated_run_manager.create_run(
                    tenant_id=self._tenant_id,
                    worker_id=self._worker_id,
                    task_description=action.task_description,
                    main_session_key=self._main_session_runtime.session_key,
                    preferred_skill_ids=action.preferred_skill_ids,
                    provenance=provenance,
                    pre_script=pre_script,
                    gate_level=gate_level,
                )
                if getattr(manifest, "status", None) != TaskStatus.ERROR:
                    task_refs.append(manifest.task_id)
                continue
            if self._worker_scheduler is None:
                manifest = create_task_manifest(
                    worker_id=self._worker_id,
                    tenant_id=self._tenant_id,
                    preferred_skill_ids=action.preferred_skill_ids,
                    provenance=provenance,
                    gate_level=gate_level,
                    task_description=action.task_description,
                    pre_script=pre_script,
                    main_session_key=self._main_session_runtime.session_key,
                ).mark_error("Worker scheduler is not available")
                if self._task_store is not None:
                    self._task_store.save(manifest)
                await self._main_session_runtime.append_message(
                    ChatMessage(
                        role="system",
                        content=(
                            "[Heartbeat 调度失败] 派生任务未提交执行，"
                            "因为 worker scheduler 不可用。"
                            f" 任务内容: {action.task_description}"
                        ),
                    )
                )
                continue
            manifest = create_task_manifest(
                worker_id=self._worker_id,
                tenant_id=self._tenant_id,
                preferred_skill_ids=action.preferred_skill_ids,
                provenance=provenance,
                gate_level=gate_level,
                task_description=action.task_description,
                pre_script=pre_script,
                main_session_key=self._main_session_runtime.session_key,
            )
            if self._task_store is not None:
                self._task_store.save(manifest)
            if main_session is None:
                main_session = await self._main_session_runtime.get_session()
            accepted = await self._worker_scheduler.submit_task(
                {
                    "task": action.task_description,
                    "tenant_id": self._tenant_id,
                    "worker_id": self._worker_id,
                    "manifest": manifest,
                    "session_id": getattr(main_session, "session_id", ""),
                    "thread_id": getattr(
                        main_session,
                        "thread_id",
                        self._main_session_runtime.session_key,
                    ),
                    "main_session_key": self._main_session_runtime.session_key,
                    "preferred_skill_ids": action.preferred_skill_ids,
                },
                priority=max(10, 20 - item.priority_hint),
            )
            if accepted:
                task_refs.append(manifest.task_id)
            elif self._task_store is not None:
                self._task_store.save(manifest.mark_error("Scheduler quota exhausted"))
        return task_refs

    def _build_task_provenance(self, item: InboxItem) -> TaskProvenance:
        source_type = (
            "goal_followup"
            if item.event_type == "goal.health_check_detected"
            else "heartbeat"
        )
        return TaskProvenance(
            source_type=source_type,
            source_id=item.inbox_id,
            goal_id=str(item.payload.get("goal_id", "") or ""),
        )

    async def _split_actionable_items(
        self,
        items: tuple[InboxItem, ...],
    ) -> tuple[list[InboxItem], list[InboxItem]]:
        actionable: list[InboxItem] = []
        suppressed: list[InboxItem] = []
        for item in items:
            if item.dedupe_key and await self._attention_ledger.has_notified(
                item.dedupe_key
            ):
                suppressed.append(item)
                continue
            actionable.append(item)
        return actionable, suppressed

    async def _mark_notifications(self, items: list[InboxItem]) -> None:
        for item in items:
            if not item.dedupe_key:
                continue
            summary = self._strategy.summarize_item(item)
            await self._attention_ledger.record_notification(
                item.dedupe_key,
                summary,
            )

    async def _on_inbox_written(self, event: Event) -> None:
        payload = dict(event.payload)
        if payload.get("worker_id") != self._worker_id:
            return
        logger.debug(
            "[HeartbeatRunner] Inbox item written for worker %s: %s",
            self._worker_id,
            payload.get("inbox_id", ""),
        )

    def _build_digest(self, items: list[InboxItem]) -> str:
        lines = [
            "你正在执行主会话 heartbeat 回合。",
            "请根据以下 inbox 事实做简洁编排：哪些需要提醒、哪些需要继续关注、哪些只是记录。",
            "",
            "Inbox Digest:",
        ]
        for index, item in enumerate(items, 1):
            payload = json.dumps(item.payload, ensure_ascii=False, sort_keys=True)
            lines.append(
                f"{index}. [{item.source_type}/{item.event_type}] "
                f"priority={item.priority_hint} dedupe={item.dedupe_key or '-'} payload={payload}"
            )
        lines.append("")
        lines.append("输出要求：给出一段简洁总结，并指出是否需要后续 follow-up。")
        return "\n".join(lines)

    def _concern_key(self, item: InboxItem) -> str:
        concern = item.payload.get("concern_key", "")
        if isinstance(concern, str) and concern.strip():
            return concern.strip()
        return item.dedupe_key
