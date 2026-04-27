# edition: baseline
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.conversation.models import ChatMessage
from src.conversation.session_manager import SessionManager
from src.memory.orchestrator import MemoryOrchestrator
from src.memory.provider import MemoryProvider, MemoryRecallResult


class _Store:
    def __init__(self):
        self.saved = {}
        self.deleted = []

    async def save(self, session):
        self.saved[session.session_id] = session

    async def delete(self, session_id):
        self.deleted.append(session_id)

    async def find_by_thread(self, thread_id):
        for session in self.saved.values():
            if session.thread_id == thread_id:
                return session
        return None

    async def list_sessions(self, tenant_id, worker_id):
        return tuple(self.saved.values())


class _Provider(MemoryProvider):
    name = "semantic"

    def __init__(self):
        self.calls = []

    async def query(self, text: str, worker_id: str, **kwargs):
        return MemoryRecallResult(source=self.name)

    async def on_session_end(self, messages):
        self.calls.append(messages)


@pytest.mark.asyncio
async def test_cleanup_expired_broadcasts_on_session_end():
    store = _Store()
    manager = SessionManager(store=store, ttl_seconds=1)
    provider = _Provider()
    manager.set_memory_orchestrator(MemoryOrchestrator((provider,)))

    session = await manager.get_or_create("thread-1", "demo", "w1")
    session = session.append_message(ChatMessage(role="user", content="hello"))
    session = replace(
        session,
        last_active_at=(datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
    )
    await manager.save(session)

    cleaned = await manager.cleanup_expired()

    assert cleaned == 1
    assert len(provider.calls) == 1
    assert provider.calls[0][0]["content"] == "hello"
    assert store.deleted == [session.session_id]
