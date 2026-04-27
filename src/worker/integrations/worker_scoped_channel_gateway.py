"""Worker-scoped outbound channel gateway for integrations."""
from __future__ import annotations

from typing import Any

from src.channels.models import ReplyContent
from src.channels.outbound import (
    DingTalkChannelAdapter,
    DirectEmailAdapter,
    FeishuChannelAdapter,
    SlackChannelAdapter,
    WeComChannelAdapter,
)
from src.channels.outbound_types import ChannelMessage, SenderScope
from src.common.logger import get_logger
from src.services.worker_platform_client_factory import WorkerPlatformClientFactory

logger = get_logger()


class WorkerScopedChannelGateway:
    """Resolve outbound adapters by tenant/worker/channel at call time."""

    def __init__(
        self,
        platform_client_factory: WorkerPlatformClientFactory,
        mount_manager: Any,
        tool_executor: Any | None,
        im_channel_registry: Any | None,
        event_bus: Any | None,
    ) -> None:
        self._platform_client_factory = platform_client_factory
        self._mount_manager = mount_manager
        self._tool_executor = tool_executor
        self._im_channel_registry = im_channel_registry
        self._event_bus = event_bus
        self._adapter_cache: dict[tuple[str, str, str], Any | None] = {}

    async def send(self, message: ChannelMessage) -> str:
        if message.im_chat_id:
            sent = await self._send_via_registry(message)
            if sent:
                return sent

        scope = _scope_from_message(message)
        adapter = self._get_adapter(
            scope.tenant_id,
            scope.worker_id,
            _normalize_channel_type(message.channel),
        )
        if adapter is None:
            raise RuntimeError(
                f"missing outbound adapter for {message.channel} "
                f"worker {scope.tenant_id}/{scope.worker_id}"
            )
        return await adapter.send(message)

    async def update_document(
        self,
        path: str,
        content: str,
        section: str | None = None,
        *,
        scope: SenderScope,
    ) -> bool:
        adapter = self._get_adapter(scope.tenant_id, scope.worker_id, "feishu")
        if adapter is None:
            return False
        return await adapter.update_document(path, content, section)

    def invalidate(
        self,
        tenant_id: str | None = None,
        worker_id: str | None = None,
    ) -> None:
        if tenant_id is None and worker_id is None:
            self._adapter_cache.clear()
            return
        for key in tuple(self._adapter_cache):
            if tenant_id is not None and key[0] != tenant_id:
                continue
            if worker_id is not None and key[1] != worker_id:
                continue
            self._adapter_cache.pop(key, None)

    async def _send_via_registry(self, message: ChannelMessage) -> str:
        if self._im_channel_registry is None or not message.im_chat_id:
            return ""
        adapter = self._im_channel_registry.find_by_chat_id(message.im_chat_id)
        if adapter is None:
            return ""
        expected_channel = _normalize_channel_type(message.channel)
        if getattr(adapter, "channel_type", "") != expected_channel:
            raise RuntimeError(
                f"IM adapter '{getattr(adapter, 'channel_type', '')}' "
                f"does not match expected channel '{expected_channel}'"
            )
        return await adapter.send_message(
            message.im_chat_id,
            ReplyContent(text=message.content),
        )

    def _get_adapter(
        self,
        tenant_id: str,
        worker_id: str,
        channel_type: str,
    ) -> Any | None:
        cache_key = (tenant_id, worker_id, channel_type)
        if cache_key in self._adapter_cache:
            return self._adapter_cache[cache_key]

        client = self._platform_client_factory.get_client(
            tenant_id,
            worker_id,
            channel_type,
        )
        adapter: Any | None = None
        if client is None:
            adapter = None
        elif channel_type == "email":
            adapter = DirectEmailAdapter(client)
        elif channel_type == "feishu":
            adapter = FeishuChannelAdapter(self._mount_manager, client)
        elif channel_type == "wecom":
            adapter = WeComChannelAdapter(client)
        elif channel_type == "dingtalk":
            adapter = DingTalkChannelAdapter(client)
        elif channel_type == "slack":
            adapter = SlackChannelAdapter(client)
        self._adapter_cache[cache_key] = adapter
        return adapter


def _scope_from_message(message: ChannelMessage) -> SenderScope:
    tenant_id = str(message.sender_tenant_id or "").strip()
    worker_id = str(message.sender_worker_id or "").strip()
    if not tenant_id or not worker_id:
        raise RuntimeError(f"{message.channel} send requires sender scope")
    return SenderScope(tenant_id=tenant_id, worker_id=worker_id)


def _normalize_channel_type(channel: str) -> str:
    normalized = str(channel or "").strip().lower()
    if normalized in {"feishu_doc", "feishu_document"}:
        return "feishu"
    return normalized
