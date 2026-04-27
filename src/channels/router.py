"""Route inbound IM messages into conversation or sensor processing."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import re
import shlex
from typing import Any, AsyncGenerator

from src.channels.dedup import MessageDeduplicator
from src.channels.commands import CommandContext
from src.common.logger import get_logger
from src.common.tenant import TrustLevel
from src.conversation.models import ChatMessage
from src.events.models import EventBusProtocol, Subscription
from src.runtime.channel_runtime import (
    StreamCollector as _StreamCollector,
    append_reply_to_session as _append_reply_to_session,
    build_sensed_fact as _build_sensed_fact,
    build_task_completed_reply as _build_task_completed_reply,
    build_task_failed_reply as _build_task_failed_reply,
    build_task_context as _build_task_context,
    build_thread_id as _build_thread_id,
    collect_reply as _collect_reply,
    extract_chat_id as _extract_chat_id,
    load_session as _load_session,
    prepare_stream as _prepare_stream,
    should_route_to_sensor as _should_route_to_sensor,
)

from .models import (
    ChannelBinding,
    ChannelInboundMessage,
    ReplyContent,
    StreamChunk,
    freeze_data,
    thaw_data,
)
from .registry import IMChannelRegistry

logger = get_logger()

_CROSS_CHANNEL_LOOKUP_STATUS_KEY = "cross_channel_lookup_status"
_CROSS_CHANNEL_LOOKUP_QUERY_KEY = "cross_channel_lookup_query"
_CROSS_CHANNEL_LOOKUP_AWAITING = "awaiting_confirmation"
_CROSS_CHANNEL_CONFIRM_PROMPT = (
    "这条消息看起来像是在继续之前聊过的话题。"
    "如果这是你之前在其他渠道里沟通过的同一件事，"
    "回复“是”我可以按关键词回查历史记录。"
)
_CROSS_CHANNEL_CONTINUATION_MARKERS = (
    "之前", "上次", "继续", "接着", "还是那个", "那个", "这件事",
    "这个事", "这个问题", "按之前", "按上次", "后续", "跟进", "延续",
)
_CROSS_CHANNEL_AFFIRMATIVE_MARKERS = (
    "是", "是的", "对", "对的", "嗯", "嗯嗯", "需要", "查一下", "去查",
    "查吧", "回查", "是同一个", "是同一件事",
)
_CROSS_CHANNEL_NEGATIVE_MARKERS = (
    "不是", "不用", "不需要", "不用查", "不用回查", "不是同一个",
)
_CROSS_CHANNEL_STOP_WORDS = frozenset({
    "之前", "上次", "继续", "接着", "那个", "这个", "事情", "这件事",
    "这个事", "问题", "处理", "处理一下", "帮忙", "麻烦", "看下", "看一下",
    "同步", "后续", "跟进", "还是", "按之前", "按上次", "一下",
})


class ChannelMessageRouter:
    """Map inbound channel messages to worker conversations."""

    def __init__(
        self,
        session_manager: Any,
        worker_router: Any,
        registry: IMChannelRegistry,
        bindings: tuple[ChannelBinding, ...],
        tenant_loader: Any | None = None,
        command_registry: Any | None = None,
        command_parser: Any | None = None,
        command_dispatcher: Any | None = None,
        sensor_registries: dict[str, Any] | None = None,
        event_bus: EventBusProtocol | None = None,
        deduplicator: MessageDeduplicator | None = None,
        contact_extractors: dict[str, Any] | None = None,
        suggestion_store: Any | None = None,
        feedback_store: Any | None = None,
        trigger_managers: dict[str, Any] | None = None,
        inbox_store: Any | None = None,
        worker_schedulers: dict[str, Any] | None = None,
        task_store: Any | None = None,
        workspace_root: str | None = None,
        llm_client: Any | None = None,
        lifecycle_services: Any | None = None,
        session_search_index: Any | None = None,
        engine_dispatcher: Any | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._worker_router = worker_router
        self._registry = registry
        self._tenant_loader = tenant_loader
        self._command_registry = command_registry
        self._command_parser = command_parser
        self._command_dispatcher = command_dispatcher
        self._event_bus = event_bus
        self._deduplicator = deduplicator
        self._sensor_registries = sensor_registries or {}
        self._contact_extractors = contact_extractors or {}
        self._suggestion_store = suggestion_store
        self._feedback_store = feedback_store
        self._inbox_store = inbox_store
        self._trigger_managers = trigger_managers or {}
        self._worker_schedulers = worker_schedulers or {}
        self._task_store = task_store
        self._workspace_root = workspace_root
        self._llm_client = llm_client
        self._lifecycle_services = lifecycle_services
        self._session_search_index = session_search_index
        self._engine_dispatcher = engine_dispatcher
        self._bindings = bindings
        self._subscription_keys: list[tuple[str, str]] = []
        self._binding_index: dict[tuple[str, str], ChannelBinding] = {}
        for binding in bindings:
            for chat_id in binding.chat_ids:
                self._binding_index[(binding.channel_type, chat_id)] = binding
        self._subscribe_task_events()

    @property
    def bindings(self) -> tuple[ChannelBinding, ...]:
        return self._bindings

    def replace_bindings(self, bindings: tuple[ChannelBinding, ...]) -> None:
        self._bindings = bindings
        self._binding_index = {}
        for binding in bindings:
            for chat_id in binding.chat_ids:
                self._binding_index[(binding.channel_type, chat_id)] = binding
        self._resubscribe_task_events()

    def replace_sensor_registries(self, sensor_registries: dict[str, Any] | None) -> None:
        self._sensor_registries = sensor_registries or {}

    def replace_contact_extractors(
        self,
        contact_extractors: dict[str, Any] | None,
    ) -> None:
        """Refresh worker-scoped contact extractors after contact registry reload."""
        self._contact_extractors = contact_extractors or {}

    def replace_worker_router(self, worker_router: Any) -> None:
        """Refresh the worker router used for message dispatch."""
        self._worker_router = worker_router

    def replace_runtime_dependencies(
        self,
        *,
        suggestion_store: Any | None = None,
        feedback_store: Any | None = None,
        inbox_store: Any | None = None,
        trigger_managers: dict[str, Any] | None = None,
        worker_schedulers: dict[str, Any] | None = None,
        task_store: Any | None = None,
        llm_client: Any | None = None,
        lifecycle_services: Any | None = None,
        session_search_index: Any | None = None,
        engine_dispatcher: Any | None = None,
    ) -> None:
        """Refresh mutable runtime dependencies after bootstrap-time changes."""
        self._suggestion_store = suggestion_store
        self._feedback_store = feedback_store
        self._inbox_store = inbox_store
        self._trigger_managers = trigger_managers or {}
        self._worker_schedulers = worker_schedulers or {}
        self._task_store = task_store
        self._llm_client = llm_client
        self._lifecycle_services = lifecycle_services
        self._session_search_index = session_search_index
        if engine_dispatcher is not None:
            self._engine_dispatcher = engine_dispatcher

    def close(self) -> None:
        if self._event_bus is None:
            return
        unsubscribe = getattr(self._event_bus, "unsubscribe", None)
        if callable(unsubscribe):
            for tenant_id, handler_id in self._subscription_keys:
                unsubscribe(tenant_id, handler_id)
        self._subscription_keys.clear()

    async def dispatch(self, message: ChannelInboundMessage) -> None:
        binding = self._resolve_binding(message.channel_type, message.chat_id)
        if binding is None:
            logger.warning(
                "[ChannelMessageRouter] No binding for %s:%s",
                message.channel_type,
                message.chat_id,
            )
            return

        if await self._try_dispatch_command(message, binding):
            return

        if message.chat_type == "p2p" or any(mention.is_bot for mention in message.mentions):
            await self.on_message(message)
            return

        if _should_route_to_sensor(binding, message):
            await self._route_to_sensor(message, binding)
            return

        logger.debug(
            "[ChannelMessageRouter] Ignored non-dialog message %s from %s",
            message.message_id,
            message.chat_id,
        )

    async def dispatch_command(
        self,
        message: ChannelInboundMessage,
        *,
        binding: ChannelBinding,
    ) -> ReplyContent | None:
        handled, content = await self._execute_command(
            message,
            binding,
            adapter=None,
        )
        return content if handled else None

    async def on_message(self, message: ChannelInboundMessage) -> None:
        if await self._is_duplicate_message(message):
            logger.info(
                "[ChannelMessageRouter] Duplicate message ignored: %s:%s",
                message.channel_type,
                message.message_id,
            )
            return

        binding = self._resolve_binding(message.channel_type, message.chat_id)
        if binding is None:
            return

        if message.channel_type == "email":
            asyncio.create_task(self._discover_email_contacts(message, binding))

        adapter = self._registry.find_by_chat_id(message.chat_id)
        if adapter is None:
            logger.warning(
                "[ChannelMessageRouter] No adapter registered for chat %s",
                message.chat_id,
            )
            return

        thread_id = _build_thread_id(message, binding)
        session_metadata = {
            "channel_type": message.channel_type,
            "chat_id": message.chat_id,
            "sender_id": message.sender_id,
            "sender_name": message.sender_name,
        }
        for key, value in message.metadata:
            session_metadata[str(key)] = str(value)
        session = await self._session_manager.get_or_create(
            thread_id=thread_id,
            tenant_id=binding.tenant_id,
            worker_id=binding.worker_id,
            metadata=session_metadata,
        )
        session = session.append_message(ChatMessage(role="user", content=message.content))

        handled, session = await self._maybe_handle_cross_channel_lookup(
            message=message,
            binding=binding,
            session=session,
            adapter=adapter,
            thread_id=thread_id,
        )
        if handled:
            return

        reply_content = ReplyContent(text="")
        spawned_task_ids: list[str] = []
        task_context = _build_task_context(message)
        effective_task = message.content
        task_context, effective_task, session = await self._resolve_cross_channel_context(
            message=message,
            binding=binding,
            session=session,
            task_context=task_context,
            thread_id=thread_id,
        )
        try:
            if binding.reply_mode == "streaming" and adapter.supports_streaming():
                chunks_gen, collector = await _prepare_stream(
                    self._worker_router.route_stream(
                        task=effective_task,
                        tenant_id=binding.tenant_id,
                        worker_id=binding.worker_id,
                        task_context=task_context,
                        conversation_session=session,
                    )
                )
                await adapter.reply_stream(message, chunks_gen)
                reply_content = collector.reply_content
                spawned_task_ids.extend(collector.spawned_task_ids)
            else:
                reply_content, task_ids = await _collect_reply(
                    self._worker_router.route_stream(
                        task=effective_task,
                        tenant_id=binding.tenant_id,
                        worker_id=binding.worker_id,
                        task_context=task_context,
                        conversation_session=session,
                    )
                )
                spawned_task_ids.extend(task_ids)
                await adapter.reply(message, reply_content)
        except Exception as exc:
            logger.error("[ChannelMessageRouter] Failed to handle message: %s", exc, exc_info=True)
            error_text = f"处理消息失败: {exc}"
            reply_content = ReplyContent(text=error_text)
            try:
                await adapter.reply(message, reply_content)
            except Exception:
                logger.error("[ChannelMessageRouter] Failed to send error reply", exc_info=True)

        session = _append_reply_to_session(session, reply_content, spawned_task_ids)
        await self._session_manager.save(session)

    async def _route_to_sensor(
        self,
        message: ChannelInboundMessage,
        binding: ChannelBinding,
    ) -> None:
        sensor_registry = self._sensor_registries.get(binding.worker_id)
        if sensor_registry is None:
            logger.debug("[ChannelMessageRouter] No sensor registry for %s", binding.worker_id)
            return

        fact = _build_sensed_fact(message)
        await sensor_registry.on_facts_sensed((fact,), message.channel_type)

    def _resolve_binding(self, channel_type: str, chat_id: str) -> ChannelBinding | None:
        return self._binding_index.get((channel_type, chat_id))

    async def _is_duplicate_message(self, message: ChannelInboundMessage) -> bool:
        if self._deduplicator is None or not message.message_id:
            return False
        return await self._deduplicator.is_duplicate(
            message.channel_type,
            message.message_id,
        )

    def _subscribe_task_events(self) -> None:
        if self._event_bus is None:
            return

        tenant_ids = {binding.tenant_id for binding in self._bindings if binding.tenant_id}
        for tenant_id in tenant_ids:
            handler_id = f"channel_router_task_completed:{tenant_id}"
            self._event_bus.subscribe(Subscription(
                handler_id=handler_id,
                event_type="task.completed",
                tenant_id=tenant_id,
                handler=self._on_task_completed,
            ))
            self._subscription_keys.append((tenant_id, handler_id))
            failed_handler_id = f"channel_router_task_failed:{tenant_id}"
            self._event_bus.subscribe(Subscription(
                handler_id=failed_handler_id,
                event_type="task.failed",
                tenant_id=tenant_id,
                handler=self._on_task_failed,
            ))
            self._subscription_keys.append((tenant_id, failed_handler_id))

    def _resubscribe_task_events(self) -> None:
        self.close()
        self._subscribe_task_events()

    async def _on_task_completed(self, event) -> None:
        payload = dict(getattr(event, "payload", ()))
        thread_id = str(payload.get("thread_id", "")).strip()
        if not thread_id.startswith("im:"):
            return
        chat_id = _extract_chat_id(thread_id)
        if not chat_id:
            return
        adapter = self._registry.find_by_chat_id(chat_id)
        if adapter is None:
            return
        description = str(payload.get("description", "")).strip() or str(payload.get("task_id", "")).strip()
        if not description:
            description = "后台任务"
        session = await _load_session(self._session_manager, thread_id)
        await adapter.send_message(
            chat_id,
            _build_task_completed_reply(description, session),
        )

    async def _on_task_failed(self, event) -> None:
        payload = dict(getattr(event, "payload", ()))
        thread_id = str(payload.get("thread_id", "")).strip()
        if not thread_id.startswith("im:"):
            return
        chat_id = _extract_chat_id(thread_id)
        if not chat_id:
            return
        adapter = self._registry.find_by_chat_id(chat_id)
        if adapter is None:
            return
        description = str(payload.get("task_id", "")).strip() or "后台任务"
        error_message = str(payload.get("error_message", "")).strip() or "unknown error"
        session = await _load_session(self._session_manager, thread_id)
        await adapter.send_message(
            chat_id,
            _build_task_failed_reply(description, error_message, session),
        )

    async def _discover_email_contacts(
        self,
        message: ChannelInboundMessage,
        binding: ChannelBinding,
    ) -> None:
        extractor = self._contact_extractors.get(binding.worker_id)
        if extractor is None:
            return
        try:
            raw_event = thaw_data(message.raw_event)
            payload = raw_event if isinstance(raw_event, dict) else {}
            payload["from"] = message.sender_id
            payload["content"] = message.content
            await extractor.extract_from_email(payload)
        except Exception as exc:
            logger.debug("[ChannelMessageRouter] Email contact discovery failed: %s", exc)

    async def _maybe_handle_cross_channel_lookup(
        self,
        *,
        message: ChannelInboundMessage,
        binding: ChannelBinding,
        session,
        adapter,
        thread_id: str,
    ) -> tuple[bool, Any]:
        if self._session_search_index is None:
            return False, session
        if not self._cross_channel_lookup_enabled(binding):
            return False, session

        metadata = dict(getattr(session, "metadata", ()))
        if metadata.get(_CROSS_CHANNEL_LOOKUP_STATUS_KEY):
            return False, session

        if not _should_offer_cross_channel_lookup(
            message=message,
            binding=binding,
            message_count=len(session.messages),
        ):
            return False, session

        query = _normalize_cross_channel_query(message, binding=binding)
        if not query:
            return False, session

        search_summary = await self._build_cross_channel_history_context(
            query=query,
            tenant_id=binding.tenant_id,
            worker_id=binding.worker_id,
            current_thread_id=thread_id,
            max_items=1,
        )
        if not search_summary:
            return False, session

        reply_content = ReplyContent(
            text=_cross_channel_confirm_prompt(binding)
        )
        await adapter.reply(message, reply_content)
        session = session.append_message(
            ChatMessage(role="assistant", content=reply_content.text)
        )
        session = _replace_session_metadata(
            session,
            {
                _CROSS_CHANNEL_LOOKUP_STATUS_KEY: _CROSS_CHANNEL_LOOKUP_AWAITING,
                _CROSS_CHANNEL_LOOKUP_QUERY_KEY: query,
            },
        )
        await self._session_manager.save(session)
        return True, session

    async def _resolve_cross_channel_context(
        self,
        *,
        message: ChannelInboundMessage,
        binding: ChannelBinding,
        session,
        task_context: str,
        thread_id: str,
    ) -> tuple[str, str, Any]:
        metadata = dict(getattr(session, "metadata", ()))
        status = str(metadata.get(_CROSS_CHANNEL_LOOKUP_STATUS_KEY, "")).strip()
        original_query = str(metadata.get(_CROSS_CHANNEL_LOOKUP_QUERY_KEY, "")).strip()
        if status != _CROSS_CHANNEL_LOOKUP_AWAITING or not original_query:
            return task_context, message.content, session

        session = _clear_cross_channel_lookup_state(session)
        if _is_negative_cross_channel_reply(message.content):
            return task_context, message.content, session

        if not _is_affirmative_cross_channel_reply(message.content):
            return task_context, message.content, session

        history_context = await self._build_cross_channel_history_context(
            query=original_query,
            tenant_id=binding.tenant_id,
            worker_id=binding.worker_id,
            current_thread_id=thread_id,
        )
        if history_context:
            task_context = "\n".join(
                part for part in (task_context, history_context) if part
            )
        return task_context, _build_cross_channel_followup_task(
            original_query=original_query,
            current_message=message.content,
        ), session

    async def _build_cross_channel_history_context(
        self,
        *,
        query: str,
        tenant_id: str,
        worker_id: str,
        current_thread_id: str,
        max_items: int = 3,
    ) -> str:
        if self._session_search_index is None:
            return ""
        if not self._cross_channel_lookup_enabled_by_ids(
            tenant_id=tenant_id,
            worker_id=worker_id,
        ):
            return ""
        fts_query = _build_cross_channel_fts_query(query)
        if not fts_query:
            return ""
        try:
            result = await self._session_search_index.search(
                query=fts_query,
                tenant_id=tenant_id,
                worker_id=worker_id,
                limit=max(max_items * 3, 6),
            )
        except Exception as exc:
            logger.debug("[ChannelMessageRouter] Cross-channel search failed: %s", exc)
            return ""

        lines = ["[Potential Cross-Channel History]"]
        seen_threads: set[str] = set()
        for hit in result.hits:
            if hit.thread_id == current_thread_id or hit.thread_id in seen_threads:
                continue
            seen_threads.add(hit.thread_id)
            snippet = _compact_search_snippet(hit.snippet or hit.content)
            if not snippet:
                continue
            lines.append(f"- {hit.created_at[:10]} {snippet}")
            if len(seen_threads) >= max_items:
                break
        return "\n".join(lines) if len(lines) > 1 else ""

    async def _try_dispatch_command(
        self,
        message: ChannelInboundMessage,
        binding: ChannelBinding,
    ) -> bool:
        adapter = self._registry.find_by_chat_id(message.chat_id)
        handled, content = await self._execute_command(
            message,
            binding,
            adapter=adapter,
        )
        if not handled:
            return False
        if adapter is not None and content is not None:
            await adapter.reply(message, content)
        return True

    async def _execute_command(
        self,
        message: ChannelInboundMessage,
        binding: ChannelBinding,
        *,
        adapter: Any | None,
    ) -> tuple[bool, ReplyContent | None]:
        if (
            self._tenant_loader is None
            or self._command_parser is None
            or self._command_dispatcher is None
        ):
            return False, None
        tenant = self._tenant_loader.load(binding.tenant_id)
        prefix = str(binding.features_dict.get("command_prefix", "/"))
        command_name = _extract_prefixed_command_name(message.content, prefix)
        if not command_name:
            return False, None
        match = self._command_parser.try_parse(
            text=message.content,
            prefix=prefix,
            channel_type=message.channel_type,
            trust_level=tenant.trust_level.name,
        )
        adapter = adapter or self._registry.find_by_adapter_id(binding.adapter_id)
        if adapter is None and message.chat_id:
            adapter = self._registry.find_by_chat_id(message.chat_id)
        if adapter is None:
            return False, None
        mentioned = message.chat_type == "p2p" or any(item.is_bot for item in message.mentions)
        if match is None:
            spec = self._command_registry.resolve(command_name.lower()) if self._command_registry else None
            if spec is None:
                return True, ReplyContent(text=f"未知命令: {prefix}{command_name}")
            if spec.visibility and message.channel_type not in spec.visibility:
                return True, ReplyContent(text=f"命令 /{spec.name} 当前渠道不可用。")
            if _command_trust_value(tenant.trust_level.name) < _command_trust_value(spec.required_trust_level):
                return True, ReplyContent(text=f"命令 /{spec.name} 当前租户权限不足，无法执行。")
            if spec.require_mention and not mentioned:
                return True, ReplyContent(text=f"命令 /{spec.name} 需要在群聊中 @机器人 后执行。")
            return False, None

        if match.spec.require_mention and not mentioned:
            return True, ReplyContent(text=f"命令 /{match.spec.name} 需要在群聊中 @机器人 后执行。")
        thread_id = _build_thread_id(message, binding)
        content = await self._command_dispatcher.execute(
            match,
            CommandContext(
                message=message,
                binding=binding,
                tenant=tenant,
                args=match.args,
                session_manager=self._session_manager,
                thread_id=thread_id,
                registry=self._command_registry,
                event_bus=self._event_bus,
                suggestion_store=self._suggestion_store,
                feedback_store=self._feedback_store,
                inbox_store=getattr(self, "_inbox_store", None),
                trigger_managers=self._trigger_managers,
                worker_schedulers=getattr(self, "_worker_schedulers", None),
                task_store=getattr(self, "_task_store", None),
                workspace_root=self._workspace_root,
                llm_client=self._llm_client,
                lifecycle_services=getattr(self, "_lifecycle_services", None),
                worker_router=self._worker_router,
                engine_dispatcher=getattr(self, "_engine_dispatcher", None),
            ),
        )
        return True, content

    def _cross_channel_lookup_enabled(self, binding: ChannelBinding) -> bool:
        return self._cross_channel_lookup_enabled_by_ids(
            tenant_id=binding.tenant_id,
            worker_id=binding.worker_id,
        )

    def _cross_channel_lookup_enabled_by_ids(
        self,
        *,
        tenant_id: str,
        worker_id: str,
    ) -> bool:
        if self._session_search_index is None:
            return False
        tenant_loader = self._tenant_loader
        worker_router = self._worker_router
        if tenant_loader is None or worker_router is None:
            return True
        resolve_entry = getattr(worker_router, "resolve_entry", None)
        if not callable(resolve_entry):
            return True
        try:
            tenant = tenant_loader.load(tenant_id)
            entry = resolve_entry(task="", tenant_id=tenant_id, worker_id=worker_id)
        except Exception as exc:
            logger.debug(
                "[ChannelMessageRouter] Cross-channel lookup trust check failed: %s",
                exc,
            )
            return False
        if entry is None:
            return False
        from src.worker.trust_gate import compute_trust_gate

        trust_gate = compute_trust_gate(entry.worker, tenant)
        return bool(getattr(trust_gate, "semantic_search_enabled", False))


def _replace_session_metadata(session, updates: dict[str, str]) -> Any:
    merged = dict(getattr(session, "metadata", ()))
    for key, value in updates.items():
        if value:
            merged[str(key)] = str(value)
        else:
            merged.pop(str(key), None)
    return replace(
        session,
        metadata=tuple((str(key), str(value)) for key, value in merged.items()),
    )


def _clear_cross_channel_lookup_state(session) -> Any:
    return _replace_session_metadata(
        session,
        {
            _CROSS_CHANNEL_LOOKUP_STATUS_KEY: "",
            _CROSS_CHANNEL_LOOKUP_QUERY_KEY: "",
        },
    )


def _extract_prefixed_command_name(text: str, prefix: str) -> str:
    stripped = str(text or "").strip()
    if not prefix or not stripped.startswith(prefix):
        return ""
    body = stripped[len(prefix):].strip()
    if not body:
        return ""
    try:
        parts = shlex.split(body)
    except ValueError:
        parts = body.split()
    if not parts:
        return ""
    return str(parts[0]).strip()


def _command_trust_value(name: str) -> int:
    try:
        return int(getattr(TrustLevel, str(name).upper()))
    except Exception:
        return int(TrustLevel.BASIC)


def _should_offer_cross_channel_lookup(
    *,
    message: ChannelInboundMessage,
    binding: ChannelBinding,
    message_count: int,
) -> bool:
    if message_count != 1:
        return False
    text = _normalize_text(message.content)
    if not text or len(text) > 80:
        return _is_email_subject_lookup_candidate(message, binding)
    markers = _cross_channel_markers(binding)
    if any(marker in text for marker in markers):
        return True
    return _is_email_subject_lookup_candidate(message, binding)


def _normalize_cross_channel_query(
    message: ChannelInboundMessage,
    *,
    binding: ChannelBinding | None = None,
) -> str:
    metadata = message.metadata_dict
    subject = str(metadata.get("subject", "")).strip()
    content = _normalize_text(message.content)
    if subject and subject not in content:
        content = f"{subject} {content}".strip()

    cleaned = content
    for marker in _cross_channel_markers(binding):
        cleaned = cleaned.replace(marker, " ")
    cleaned = re.sub(r"\[[^\]]+\]", " ", cleaned)
    cleaned = re.sub(r"[\n\r\t,，。.!！？:：;；/\\]+", " ", cleaned)
    chunks = [
        part.strip()
        for part in cleaned.split(" ")
        if part.strip() and part.strip() not in _CROSS_CHANNEL_STOP_WORDS
    ]
    selected = []
    for chunk in chunks:
        if len(chunk) < 2:
            continue
        selected.append(chunk[:24])
        if len(selected) >= 4:
            break
    if not selected and subject:
        return subject[:48]
    return " ".join(selected)[:96]


def _build_cross_channel_fts_query(query: str) -> str:
    tokens = []
    for part in str(query or "").split():
        token = part.strip().strip('"').strip("'")
        if not token or len(token) < 2:
            continue
        tokens.append(token[:24])
    unique_tokens = list(dict.fromkeys(tokens))
    if not unique_tokens:
        return ""
    return " OR ".join(f'"{token}"' for token in unique_tokens[:4])


def _is_affirmative_cross_channel_reply(content: str) -> bool:
    text = _normalize_text(content)
    if not text or len(text) > 24:
        return False
    if _is_negative_cross_channel_reply(text):
        return False
    return any(marker in text for marker in _CROSS_CHANNEL_AFFIRMATIVE_MARKERS)


def _is_negative_cross_channel_reply(content: str) -> bool:
    text = _normalize_text(content)
    if not text or len(text) > 24:
        return False
    return any(marker in text for marker in _CROSS_CHANNEL_NEGATIVE_MARKERS)


def _build_cross_channel_followup_task(
    *,
    original_query: str,
    current_message: str,
) -> str:
    current = _normalize_text(current_message)
    if _is_affirmative_cross_channel_reply(current):
        return original_query
    if not current or current == original_query:
        return original_query
    return "\n".join((
        original_query,
        "",
        f"用户补充：{current}",
    ))


def _compact_search_snippet(text: str) -> str:
    normalized = _normalize_text(text)
    return normalized[:120]


def _normalize_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    return normalized


def _cross_channel_confirm_prompt(binding: ChannelBinding | None) -> str:
    if binding is None:
        return _CROSS_CHANNEL_CONFIRM_PROMPT
    value = binding.features_dict.get("cross_channel_lookup_prompt", "")
    text = _normalize_text(value)
    return text or _CROSS_CHANNEL_CONFIRM_PROMPT


def _cross_channel_markers(binding: ChannelBinding | None) -> tuple[str, ...]:
    if binding is None:
        return _CROSS_CHANNEL_CONTINUATION_MARKERS
    return _feature_markers(
        binding,
        key="cross_channel_lookup_markers",
        default=_CROSS_CHANNEL_CONTINUATION_MARKERS,
    )


def _is_email_subject_lookup_candidate(
    message: ChannelInboundMessage,
    binding: ChannelBinding | None,
) -> bool:
    if message.channel_type != "email":
        return False
    if binding is not None:
        raw = binding.features_dict.get("cross_channel_lookup_email_subject_enabled", True)
        if str(raw).strip().lower() in {"0", "false", "no"}:
            return False
    subject = _normalize_text(message.metadata_dict.get("subject", ""))
    if not subject or len(subject) > 80:
        return False
    tokens = [
        token for token in re.split(r"[\s\-_:：，,]+", subject)
        if token and len(token) >= 2
    ]
    return len(tokens) >= 2


def _feature_markers(
    binding: ChannelBinding,
    *,
    key: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    raw = binding.features_dict.get(key, default)
    if isinstance(raw, str):
        values = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        values = [str(item).strip() for item in raw]
    else:
        return default
    parsed = tuple(item for item in values if item)
    return parsed or default
