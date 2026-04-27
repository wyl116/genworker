"""
Integration bootstrap initializer.

Creates ChannelAdapters, SyncManager, and parser/shared integration services.
Priority 130: after scheduler(110) and conversation(120).
"""
import logging
from pathlib import Path

from src.autonomy.inbox import SessionInboxStore
# Re-export runtime helpers here to preserve existing bootstrap imports.
from src.runtime.integration_runtime import (
    ExternalGoalPolicy,
    coerce_bool as _coerce_bool,
    extract_webhook_content as _extract_webhook_content,
    infer_worker_id_from_goal_file as _infer_worker_id_from_goal_file,
    load_feishu_document_content as _load_feishu_document_content,
    publish_external_content_event as _publish_external_content_event,
    register_external_content_subscriptions as _register_external_content_subscriptions,
    register_goal_approval_subscription as _register_goal_approval_subscription,
    register_goal_sync_subscriptions as _register_goal_sync_subscriptions,
    resolve_external_goal_policy as _resolve_external_goal_policy,
)

from .base import Initializer

logger = logging.getLogger(__name__)


class IntegrationInitializer(Initializer):
    """
    Initialize the external integration subsystem.

    Creates:
    - ContentParser (LLM-driven content extraction)
    - ChannelAdapters (email, feishu) wrapped with ReliableChannelAdapter
    - MultiChannelFallback (degradation chain)
    - SyncManager (bidirectional sync)
    """

    def __init__(self) -> None:
        self._apscheduler = None
        self._subscriptions: list[tuple[str, str]] = []
        self._event_bus = None

    @property
    def name(self) -> str:
        return "integrations"

    @property
    def depends_on(self) -> list[str]:
        return ["scheduler", "events", "platforms", "channels"]

    @property
    def priority(self) -> int:
        return 130

    @property
    def required(self) -> bool:
        return False

    async def initialize(self, context) -> bool:
        """
        Build integration infrastructure.

        1. Get dependencies from context
        2. Create ContentParser
        3. Create ChannelAdapters with retry wrappers
        4. Create MultiChannelFallback
        5. Create SyncManager
        6. Store in context
        """
        try:
            from src.worker.integrations.content_parser import ContentParser
            from src.worker.integrations.sync_manager import SyncManager
            from src.worker.integrations.worker_scoped_channel_gateway import (
                WorkerScopedChannelGateway,
            )
            event_bus = context.get_state("event_bus")
            self._event_bus = event_bus
            self._apscheduler = context.get_state("apscheduler")
            tenant_id = context.get_state("tenant_id", "demo")
            workspace_root = Path(
                context.get_state("workspace_root", "workspace")
            )

            # Build content parser (uses LLM if available)
            llm_client = context.get_state("llm_client")
            content_parser = (
                ContentParser(llm_client) if llm_client else None
            )

            # Build channel adapters
            tool_executor = context.get_state("tool_executor")
            mount_manager = context.get_state("mount_manager")
            platform_client_factory = context.get_state("platform_client_factory")
            im_channel_registry = context.get_state("im_channel_registry")
            redis_client = context.get_state("redis_client")
            settings = context.settings
            inbox_store = SessionInboxStore(
                redis_client=redis_client,
                fallback_dir=workspace_root,
                event_bus=event_bus,
                processing_timeout_minutes=getattr(
                    settings, "heartbeat_processing_timeout_minutes", 10,
                ),
            )

            channel = WorkerScopedChannelGateway(
                platform_client_factory=platform_client_factory,
                mount_manager=mount_manager,
                tool_executor=tool_executor,
                im_channel_registry=im_channel_registry,
                event_bus=event_bus,
            )

            sync_manager = SyncManager(
                channel_adapter=channel,
                event_bus=event_bus,
                tenant_id=tenant_id,
            )
            self._subscriptions.extend(self._register_goal_sync_subscriptions(
                event_bus=event_bus,
                tenant_id=tenant_id,
                sync_manager=sync_manager,
                workspace_root=workspace_root,
            ))
            self._subscriptions.extend(self._register_external_content_subscriptions(
                context=context,
                event_bus=event_bus,
                tenant_id=tenant_id,
            ))
            self._subscriptions.extend(self._register_goal_approval_subscription(
                context=context,
                event_bus=event_bus,
                tenant_id=tenant_id,
            ))

            # Store in context
            context.set_state("content_parser", content_parser)
            context.set_state("channel_adapter", channel)
            context.set_state("sync_manager", sync_manager)
            context.set_state("integration_inbox_store", inbox_store)
            context.set_state("integration_channel_gateway", channel)

            logger.info("[IntegrationInit] Integration subsystem initialized")
            return True

        except Exception as exc:
            logger.error(
                f"[IntegrationInit] Failed: {exc}", exc_info=True,
            )
            return False

    async def cleanup(self) -> None:
        """Release subscriptions registered by the integration subsystem."""
        unsubscribe = getattr(self._event_bus, "unsubscribe", None)
        if unsubscribe is not None:
            for tenant_id, handler_id in self._subscriptions:
                try:
                    unsubscribe(tenant_id, handler_id)
                except Exception as exc:
                    logger.error(
                        f"[IntegrationInit] Failed to unsubscribe "
                        f"{handler_id}: {exc}"
                    )
        self._subscriptions.clear()
        self._event_bus = None
        logger.info("[IntegrationInit] Integration subsystem cleaned up")

    def _register_goal_sync_subscriptions(
        self,
        event_bus,
        tenant_id: str,
        sync_manager,
        workspace_root: Path | None = None,
    ) -> list[tuple[str, str]]:
        """Subscribe to goal escalation events that require stakeholder follow-up."""
        return _register_goal_sync_subscriptions(
            event_bus=event_bus,
            tenant_id=tenant_id,
            sync_manager=sync_manager,
            workspace_root=workspace_root,
        )

    def _register_external_content_subscriptions(
        self,
        *,
        context,
        event_bus,
        tenant_id: str,
    ) -> list[tuple[str, str]]:
        """Bridge sensed external content into ContentParser -> Goal generation."""
        return _register_external_content_subscriptions(
            context=context,
            event_bus=event_bus,
            tenant_id=tenant_id,
        )

    def _register_goal_approval_subscription(
        self,
        *,
        context,
        event_bus,
        tenant_id: str,
    ) -> list[tuple[str, str]]:
        """Register health checks when a pending goal is approved."""
        return _register_goal_approval_subscription(
            context=context,
            apscheduler=self._apscheduler,
            event_bus=event_bus,
            tenant_id=tenant_id,
        )


class _RegistryBackedIMSender:
    """Resolve IM adapters by chat id at send time."""

    def __init__(self, registry, expected_channel_type: str) -> None:
        self._registry = registry
        self._expected_channel_type = expected_channel_type

    async def send_message(self, chat_id: str, content) -> str:
        if self._registry is None:
            raise RuntimeError("IM channel registry not available")
        adapter = self._registry.find_by_chat_id(chat_id)
        if adapter is None:
            raise RuntimeError(f"IM adapter not found for chat '{chat_id}'")
        if getattr(adapter, "channel_type", "") != self._expected_channel_type:
            raise RuntimeError(
                f"IM adapter '{getattr(adapter, 'channel_type', '')}' "
                f"does not match expected channel '{self._expected_channel_type}'"
            )
        return await adapter.send_message(chat_id, content)
