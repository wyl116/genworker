"""Registry for IM channel adapters and chat routing indexes."""
from __future__ import annotations

from src.common.logger import get_logger

from .protocol import IMChannelAdapter

logger = get_logger()


class IMChannelRegistry:
    """Register adapters by adapter_id and reverse-index their chat ids."""

    def __init__(self) -> None:
        self._adapters: dict[str, IMChannelAdapter] = {}
        self._chat_index: dict[str, str] = {}

    def register(
        self,
        adapter_id: str,
        adapter: IMChannelAdapter,
        *,
        chat_ids: tuple[str, ...] = (),
    ) -> None:
        self.unregister(adapter_id)
        self._adapters[adapter_id] = adapter
        for chat_id in chat_ids:
            if chat_id:
                self._chat_index[chat_id] = adapter_id
        logger.info("[IMChannelRegistry] Registered %s for %s chats", adapter_id, len(chat_ids))

    def unregister(self, adapter_id: str) -> None:
        self._adapters.pop(adapter_id, None)
        for chat_id, indexed_adapter_id in tuple(self._chat_index.items()):
            if indexed_adapter_id == adapter_id:
                self._chat_index.pop(chat_id, None)

    def get_adapter(self, adapter_id: str) -> IMChannelAdapter | None:
        return self._adapters.get(adapter_id)

    def find_by_adapter_id(self, adapter_id: str) -> IMChannelAdapter | None:
        return self._adapters.get(adapter_id)

    def find_by_chat_id(self, chat_id: str) -> IMChannelAdapter | None:
        adapter_id = self._chat_index.get(chat_id)
        if not adapter_id:
            return None
        return self._adapters.get(adapter_id)

    def list_adapters(self) -> list[str]:
        return sorted(self._adapters.keys())
