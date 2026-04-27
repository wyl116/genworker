"""Lifecycle manager for IM channel adapters."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

from src.common.logger import get_logger

from .models import ChannelBinding
from .registry import IMChannelRegistry
from .router import ChannelMessageRouter

logger = get_logger()

AdapterFactory = Callable[[str, str, str, tuple[ChannelBinding, ...]], Any]


class ChannelManager:
    """Create, start, stop, and inspect IM channel adapters."""

    def __init__(
        self,
        registry: IMChannelRegistry,
        router: ChannelMessageRouter,
        bindings: tuple[ChannelBinding, ...],
        adapter_factory: AdapterFactory,
    ) -> None:
        self._registry = registry
        self._router = router
        self._bindings = bindings
        self._adapter_factory = adapter_factory

    async def start_all(self) -> None:
        _validate_chat_id_uniqueness(self._bindings)
        for key, group in _group_bindings(self._bindings).items():
            await self._start_group(key, group)

    async def stop_all(self) -> None:
        for adapter_id in self._registry.list_adapters():
            adapter = self._registry.get_adapter(adapter_id)
            if adapter is None:
                continue
            try:
                await adapter.stop()
            finally:
                self._registry.unregister(adapter_id)

    async def health_check(self) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for adapter_id in self._registry.list_adapters():
            adapter = self._registry.get_adapter(adapter_id)
            if adapter is not None:
                results[adapter_id] = await adapter.health_check()
        return results

    async def reload_worker(
        self,
        tenant_id: str,
        worker_id: str,
        bindings: tuple[ChannelBinding, ...],
    ) -> None:
        next_bindings = tuple(
            binding
            for binding in self._bindings
            if not (
                binding.tenant_id == tenant_id and binding.worker_id == worker_id
            )
        ) + tuple(bindings)
        old_groups = _group_bindings(self._bindings)
        new_groups = _group_bindings(next_bindings)
        _validate_chat_id_uniqueness(next_bindings)
        changed_keys = {
            key
            for key in set(old_groups) | set(new_groups)
            if old_groups.get(key) != new_groups.get(key)
        }

        for channel_type, group_tenant_id, group_worker_id in changed_keys:
            adapter_id = f"{channel_type}:{group_tenant_id}:{group_worker_id}"
            adapter = self._registry.get_adapter(adapter_id)
            if adapter is not None:
                await adapter.stop()
                self._registry.unregister(adapter_id)

        self._bindings = next_bindings
        self._router.replace_bindings(next_bindings)

        for key in changed_keys:
            group = new_groups.get(key)
            if group:
                await self._start_group(key, group)

    async def _start_group(
        self,
        key: tuple[str, str, str],
        group: tuple[ChannelBinding, ...],
    ) -> None:
        channel_type, tenant_id, worker_id = key
        adapter_id = f"{channel_type}:{tenant_id}:{worker_id}"
        adapter = self._adapter_factory(channel_type, tenant_id, worker_id, group)
        if adapter is None:
            logger.warning("[ChannelManager] No adapter factory for %s", adapter_id)
            return
        chat_ids = tuple({
            chat_id
            for binding in group
            for chat_id in binding.chat_ids
            if _is_indexable_chat_id(chat_id)
        })
        self._registry.register(adapter_id, adapter, chat_ids=chat_ids)
        await adapter.start(self._router.dispatch)


def _group_bindings(
    bindings: tuple[ChannelBinding, ...],
) -> dict[tuple[str, str, str], tuple[ChannelBinding, ...]]:
    groups: dict[tuple[str, str, str], list[ChannelBinding]] = defaultdict(list)
    for binding in bindings:
        groups[(binding.channel_type, binding.tenant_id, binding.worker_id)].append(binding)
    return {key: tuple(value) for key, value in groups.items()}


def _validate_chat_id_uniqueness(bindings: tuple[ChannelBinding, ...]) -> None:
    seen: dict[tuple[str, str], tuple[str, str]] = {}
    for binding in bindings:
        for chat_id in binding.chat_ids:
            if not _is_indexable_chat_id(chat_id):
                continue
            key = (binding.channel_type, chat_id)
            current = (binding.tenant_id, binding.worker_id)
            previous = seen.get(key)
            if previous is not None and previous != current:
                raise ValueError(
                    "Duplicate chat_id binding for "
                    f"channel={binding.channel_type} chat_id={chat_id}: "
                    f"{previous[0]}/{previous[1]} vs {current[0]}/{current[1]}"
                )
            seen[key] = current


def _is_indexable_chat_id(chat_id: str) -> bool:
    value = str(chat_id or "").strip()
    return bool(value) and value != "*"
