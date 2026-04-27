"""Bootstrap initializer for IM channels."""
from __future__ import annotations

from typing import Any

from src.channels.bindings import build_worker_bindings
from src.common.logger import get_logger
from src.worker.registry import WorkerRegistry

from .base import Initializer

logger = get_logger()


class ChannelInitializer(Initializer):
    """Initialize IM channel registry, router, and adapters."""

    def __init__(self) -> None:
        self._channel_manager = None

    @property
    def name(self) -> str:
        return "channels"

    @property
    def depends_on(self) -> list[str]:
        return [
            "workers",
            "api_wiring",
            "scheduler",
            "conversation",
            "platforms",
            "events",
            "contacts",
        ]

    @property
    def priority(self) -> int:
        return 129

    async def initialize(self, context) -> bool:
        from src.channels import (
            ChannelManager,
            ChannelMessageRouter,
            IMChannelRegistry,
            build_channel_binding,
        )
        from src.channels.commands import (
            CommandDispatcher,
            CommandParser,
            build_builtin_command_registry,
        )
        from src.channels.dedup import MessageDeduplicator
        from src.worker.contacts.discovery import PersonExtractor

        if not bool(getattr(context.settings, "im_channel_enabled", True)):
            context.set_state("im_channel_registry", None)
            context.set_state("channel_registry", None)
            context.set_state("channel_message_router", None)
            context.set_state("channel_router", None)
            context.set_state("command_registry", None)
            context.set_state("channel_manager", None)
            logger.info("[ChannelInit] IM channel runtime disabled by IM_CHANNEL_ENABLED=false")
            return True

        worker_registry = context.get_state("worker_registry", WorkerRegistry())
        bindings = []
        platform_client_factory = context.get_state("platform_client_factory")
        for entry in worker_registry.list_all():
            bindings.extend(build_worker_bindings(
                entry,
                tenant_id=context.get_state("tenant_id", "demo"),
                platform_client_factory=platform_client_factory,
            ))

        redis_client = context.get_state("redis_client")
        deduplicator = MessageDeduplicator(redis_client=redis_client)
        contact_registries = context.get_state("contact_registries", {})
        contact_extractors = {
            worker_id: PersonExtractor(contact_registry=registry)
            for worker_id, registry in contact_registries.items()
        }

        registry = IMChannelRegistry()
        command_registry = build_builtin_command_registry()
        router = ChannelMessageRouter(
            session_manager=context.get_state("session_manager"),
            worker_router=context.get_state("worker_router"),
            registry=registry,
            bindings=tuple(bindings),
            tenant_loader=context.get_state("tenant_loader"),
            command_registry=command_registry,
            command_parser=CommandParser(command_registry),
            command_dispatcher=CommandDispatcher(),
            sensor_registries=context.get_state("sensor_registries", {}),
            event_bus=context.get_state("event_bus"),
            deduplicator=deduplicator,
            contact_extractors=contact_extractors,
            suggestion_store=context.get_state("suggestion_store"),
            feedback_store=context.get_state("feedback_store"),
            inbox_store=(
                context.get_state("session_inbox_store")
                or context.get_state("goal_inbox_store")
                or context.get_state("integration_inbox_store")
            ),
            trigger_managers=context.get_state("trigger_managers", {}),
            worker_schedulers=context.get_state("worker_schedulers", {}),
            task_store=context.get_state("task_store"),
            workspace_root=context.get_state("workspace_root", "workspace"),
            llm_client=context.get_state("llm_client"),
            lifecycle_services=context.get_state("lifecycle_services"),
            session_search_index=context.get_state("session_search_index"),
        )

        def _factory(channel_type: str, tenant_id: str, worker_id: str, grouped_bindings):
            if platform_client_factory is None:
                return None
            client = platform_client_factory.get_client(
                tenant_id,
                worker_id,
                channel_type,
            )
            if client is None:
                logger.warning(
                    "[ChannelInit] Skip %s for worker=%s tenant=%s: missing credentials",
                    channel_type,
                    worker_id,
                    tenant_id,
                )
                return None
            if channel_type == "feishu":
                from src.channels.adapters import FeishuIMAdapter

                return FeishuIMAdapter(client, grouped_bindings)
            if channel_type == "wecom":
                from src.channels.adapters import WeComIMAdapter

                return WeComIMAdapter(client, grouped_bindings)
            if channel_type == "dingtalk":
                from src.channels.adapters import DingTalkIMAdapter

                return DingTalkIMAdapter(client, grouped_bindings)
            if channel_type == "slack":
                from src.channels.adapters import SlackIMAdapter

                return SlackIMAdapter(client, grouped_bindings)
            if channel_type == "email":
                from src.channels.adapters import EmailIMAdapter

                return EmailIMAdapter(
                    client,
                    grouped_bindings,
                    poll_config=_build_email_poll_config(grouped_bindings),
                )
            return None

        manager = ChannelManager(
            registry=registry,
            router=router,
            bindings=tuple(bindings),
            adapter_factory=_factory,
        )
        await manager.start_all()

        context.set_state("im_channel_registry", registry)
        context.set_state("channel_registry", registry)
        context.set_state("channel_message_router", router)
        context.set_state("channel_router", router)
        context.set_state("command_registry", command_registry)
        context.set_state("channel_manager", manager)
        context.set_state("message_deduplicator", deduplicator)
        context.register_runtime_component(
            "message_dedup",
            deduplicator.runtime_status,
        )
        self._channel_manager = manager
        logger.info("[ChannelInit] Initialized %s channel bindings", len(bindings))
        return True

    async def cleanup(self) -> None:
        if self._channel_manager is not None:
            router = getattr(self._channel_manager, "_router", None)
            if router is not None and hasattr(router, "close"):
                router.close()
        if self._channel_manager is not None:
            await self._channel_manager.stop_all()
def _build_email_poll_config(bindings: tuple[Any, ...]):
    from src.channels.adapters.email_adapter import EmailPollConfig

    features = bindings[0].features_dict if bindings else {}
    folders_raw = features.get("folders", ("INBOX",))
    if isinstance(folders_raw, str):
        folders = tuple(item.strip() for item in folders_raw.split(",") if item.strip()) or ("INBOX",)
    elif isinstance(folders_raw, (list, tuple)):
        folders = tuple(str(item).strip() for item in folders_raw if str(item).strip()) or ("INBOX",)
    else:
        folders = ("INBOX",)
    return EmailPollConfig(
        interval_seconds=_safe_int(features.get("poll_interval", 60), 60),
        max_fetch_per_poll=_safe_int(features.get("max_fetch_per_poll", 50), 50),
        folders=folders,
        account=str(features.get("account", "worker_mailbox")).strip() or "worker_mailbox",
    )


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
