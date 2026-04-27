# edition: baseline
import time

import pytest

from src.channels.dedup import DeduplicatorConfig, MessageDeduplicator


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.fail = False

    async def get(self, key: str):
        if self.fail:
            raise RuntimeError("redis down")
        return self.data.get(key)

    async def set(self, key: str, value: str, ttl=None, nx=False):
        if self.fail:
            raise RuntimeError("redis down")
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True


@pytest.mark.asyncio
async def test_message_deduplicator_rejects_duplicate_from_redis() -> None:
    redis = FakeRedis()
    deduplicator = MessageDeduplicator(redis_client=redis)

    assert await deduplicator.is_duplicate("feishu", "om_1") is False
    assert await deduplicator.is_duplicate("feishu", "om_1") is True


@pytest.mark.asyncio
async def test_message_deduplicator_is_namespaced_by_channel() -> None:
    redis = FakeRedis()
    deduplicator = MessageDeduplicator(redis_client=redis)

    assert await deduplicator.is_duplicate("feishu", "same-id") is False
    assert await deduplicator.is_duplicate("wecom", "same-id") is False


@pytest.mark.asyncio
async def test_message_deduplicator_falls_back_to_memory_on_redis_failure() -> None:
    redis = FakeRedis()
    redis.fail = True
    deduplicator = MessageDeduplicator(
        redis_client=redis,
        config=DeduplicatorConfig(ttl_seconds=10, memory_max_size=2),
    )

    assert await deduplicator.is_duplicate("dingtalk", "msg_1") is False
    assert await deduplicator.is_duplicate("dingtalk", "msg_1") is True
    status = deduplicator.runtime_status()
    assert status.status == "degraded" or status.status.value == "degraded"
    assert status.selected_backend == "memory"
    assert status.last_error == "redis down"


@pytest.mark.asyncio
async def test_message_deduplicator_expires_memory_entries() -> None:
    deduplicator = MessageDeduplicator(
        redis_client=None,
        config=DeduplicatorConfig(ttl_seconds=10, memory_max_size=10),
    )

    assert await deduplicator.is_duplicate("feishu", "expiring") is False
    dedup_key = deduplicator._dedup_key("feishu", "expiring")
    deduplicator._memory[dedup_key] = time.time() - 1

    assert await deduplicator.is_duplicate("feishu", "expiring") is False


def test_message_deduplicator_runtime_status_is_ready_in_memory_mode() -> None:
    deduplicator = MessageDeduplicator(redis_client=None)

    status = deduplicator.runtime_status()

    assert status.status.value == "ready"
    assert status.selected_backend == "memory"
