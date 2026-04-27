# edition: baseline
import pytest

from src.channels.dedup import MessageDeduplicator


class _FailingRedis:
    def __getattr__(self, name):
        async def _raise(*args, **kwargs):
            raise RuntimeError("redis down")

        return _raise


@pytest.mark.asyncio
async def test_dedup_runtime_status_marks_memory_mode_ready():
    dedup = MessageDeduplicator(redis_client=None)

    status = dedup.runtime_status()

    assert status.status.value == "ready"
    assert status.selected_backend == "memory"
    assert status.primary_backend == "redis"
    assert status.fallback_backend == "memory"
    assert status.ground_truth == "memory"


@pytest.mark.asyncio
async def test_dedup_runtime_status_marks_redis_fallback_as_degraded():
    dedup = MessageDeduplicator(redis_client=_FailingRedis())

    await dedup.is_duplicate("slack", "msg-1")

    status = dedup.runtime_status()

    assert status.status.value == "degraded"
    assert status.selected_backend == "memory"
    assert status.ground_truth == "memory"
    assert status.last_error == "redis down"
