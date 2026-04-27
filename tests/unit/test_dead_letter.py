# edition: baseline
import pytest

from src.worker.dead_letter import DeadLetterEntry, DeadLetterStore


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.fail = False

    async def hset(self, key: str, field=None, value=None, mapping=None):
        if self.fail:
            raise RuntimeError("redis down")
        bucket = self.hashes.setdefault(key, {})
        if mapping:
            bucket.update(mapping)
        elif field is not None and value is not None:
            bucket[field] = value
        return 1

    async def hgetall(self, key: str):
        if self.fail:
            raise RuntimeError("redis down")
        return dict(self.hashes.get(key, {}))

    async def hget(self, key: str, field: str):
        if self.fail:
            raise RuntimeError("redis down")
        return self.hashes.get(key, {}).get(field)

    async def hdel(self, key: str, *fields: str):
        if self.fail:
            raise RuntimeError("redis down")
        bucket = self.hashes.setdefault(key, {})
        removed = 0
        for field in fields:
            if field in bucket:
                removed += 1
                bucket.pop(field, None)
        return removed


def _entry(entry_id: str = "dl-1") -> DeadLetterEntry:
    return DeadLetterEntry(
        entry_id=entry_id,
        worker_id="worker-1",
        tenant_id="demo",
        task_description="do work",
        error_message="boom",
        retry_count=3,
        failed_at="2026-04-09T00:00:00+00:00",
        job_snapshot=(("task", "do work"),),
    )


@pytest.mark.asyncio
async def test_dead_letter_store_add_list_retry_discard_with_redis() -> None:
    store = DeadLetterStore(redis_client=FakeRedis(), fallback_dir="workspace")
    entry = _entry()

    await store.add(entry)
    listed = await store.list_entries("worker-1")
    retried = await store.retry("worker-1", "dl-1")
    discarded = await store.discard("worker-1", "missing")

    assert listed == (entry,)
    assert retried == entry
    assert discarded is False


@pytest.mark.asyncio
async def test_dead_letter_store_falls_back_to_file(tmp_path) -> None:
    redis = FakeRedis()
    redis.fail = True
    store = DeadLetterStore(redis_client=redis, fallback_dir=tmp_path)
    entry = _entry("dl-file")

    await store.add(entry)

    listed = await store.list_entries("worker-1")
    assert listed == (entry,)

    retried = await store.retry("worker-1", "dl-file")
    assert retried == entry
    assert await store.list_entries("worker-1") == ()
    status = store.runtime_status()
    assert status.status.value == "degraded"
    assert status.selected_backend == "file"
