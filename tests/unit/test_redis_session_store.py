# edition: baseline
import pytest

from src.conversation.models import ChatMessage, ConversationSession
from src.conversation.session_store import (
    FileSessionStore,
    HybridSessionStore,
    RedisSessionStore,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value: str, ttl=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, *keys: str):
        for key in keys:
            self.values.pop(key, None)
            self.sets.pop(key, None)
        return 1

    async def sadd(self, key: str, *values: str):
        self.sets.setdefault(key, set()).update(values)
        return len(values)

    async def srem(self, key: str, *values: str):
        bucket = self.sets.setdefault(key, set())
        removed = 0
        for value in values:
            if value in bucket:
                bucket.remove(value)
                removed += 1
        return removed

    async def smembers(self, key: str):
        return set(self.sets.get(key, set()))

    async def mget(self, *keys: str):
        return [self.values.get(key) for key in keys]


def _session(session_id: str = "s1") -> ConversationSession:
    return ConversationSession(
        session_id=session_id,
        thread_id="thread-1",
        tenant_id="demo",
        worker_id="worker-1",
        messages=(ChatMessage(role="user", content="hello"),),
        ttl_seconds=300,
    )


@pytest.mark.asyncio
async def test_redis_session_store_crud_and_indexes() -> None:
    store = RedisSessionStore(FakeRedis())
    session = _session()

    await store.save(session)

    loaded = await store.load(session.session_id)
    by_thread = await store.find_by_thread(session.thread_id)
    listed = await store.list_sessions("demo", "worker-1")

    assert loaded == session
    assert by_thread == session
    assert listed == (session,)

    await store.delete(session.session_id)
    assert await store.load(session.session_id) is None


@pytest.mark.asyncio
async def test_redis_session_store_prunes_orphan_ids() -> None:
    redis = FakeRedis()
    store = RedisSessionStore(redis)
    await store.save(_session())
    await redis.sadd("lw:session_idx:list:demo:worker-1", "missing-session")

    listed = await store.list_sessions("demo", "worker-1")

    assert len(listed) == 1
    assert "missing-session" not in redis.sets["lw:session_idx:list:demo:worker-1"]


@pytest.mark.asyncio
async def test_hybrid_session_store_backfills_redis_from_file(tmp_path) -> None:
    redis = FakeRedis()
    file_store = FileSessionStore(tmp_path)
    store = HybridSessionStore(RedisSessionStore(redis), file_store)
    session = _session("file-only")

    await file_store.save(session)

    loaded = await store.load("file-only")

    assert loaded == session
    assert await store.find_by_thread("thread-1") == session
    assert "lw:session:file-only" in redis.values
