"""Bridge langgraph interrupts into inbox items."""
from __future__ import annotations

from typing import Any, Mapping

from src.autonomy.inbox import InboxItem, SessionInboxStore
from src.channels.commands.approval_events import (
    LANGGRAPH_INTERRUPT_EVENT_TYPE,
    register_approval_event_type,
)
from src.common.logger import get_logger
from src.skills.models import NodeDefinition

from .digest import compute_state_digest

logger = get_logger()


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class InterruptBridge:
    """Create approval inbox items for paused graph nodes."""

    def __init__(
        self,
        *,
        inbox_store: SessionInboxStore,
        default_event_type: str = LANGGRAPH_INTERRUPT_EVENT_TYPE,
    ) -> None:
        self._inbox_store = inbox_store
        self._default_event_type = default_event_type

    def register_custom_event_type(self, event_type: str) -> None:
        register_approval_event_type(event_type)

    async def create_inbox(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        thread_id: str,
        skill_id: str,
        node: NodeDefinition,
        state: Mapping[str, Any],
        state_whitelist: tuple[str, ...],
        prompt_template: str,
    ) -> tuple[str, str]:
        state_digest = compute_state_digest(state, state_whitelist)
        event_type = str(node.inbox_event_type or self._default_event_type).strip()
        if not event_type:
            event_type = self._default_event_type
        self.register_custom_event_type(event_type)
        item = InboxItem(
            tenant_id=tenant_id,
            worker_id=worker_id,
            source_type="langgraph",
            event_type=event_type,
            priority_hint=25,
            payload={
                "engine": "langgraph",
                "thread_id": thread_id,
                "skill_id": skill_id,
                "node": node.name,
                "state_digest": state_digest,
                "prompt": "",
            },
        )
        stored = await self._inbox_store.write(item)
        prompt = prompt_template.format_map(
            _SafeDict({**dict(state), "inbox_id": stored.inbox_id})
        )
        payload = dict(stored.payload)
        payload["prompt"] = prompt
        updated = InboxItem.from_dict(
            {
                **stored.to_dict(),
                "payload": payload,
            }
        )
        await self._inbox_store.write(updated)
        logger.info(
            "[LangGraphInterrupt] created inbox_id=%s thread=%s node=%s",
            stored.inbox_id,
            thread_id,
            node.name,
        )
        return stored.inbox_id, prompt
