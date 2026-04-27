# edition: baseline
import pytest

from src.autonomy.inbox import InboxItem, SessionInboxStore


class _FailingRedis:
    def __getattr__(self, name):
        async def _raise(*args, **kwargs):
            raise RuntimeError("redis down")

        return _raise


@pytest.mark.asyncio
async def test_inbox_runtime_status_marks_file_mode_ready(tmp_path):
    store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)

    status = store.runtime_status()

    assert status.status.value == "ready"
    assert status.selected_backend == "file"
    assert status.primary_backend == "redis"
    assert status.fallback_backend == "file"
    assert status.ground_truth == "file"


@pytest.mark.asyncio
async def test_inbox_runtime_status_marks_redis_fallback_as_degraded(tmp_path):
    store = SessionInboxStore(redis_client=_FailingRedis(), fallback_dir=tmp_path)

    await store.write(
        InboxItem(
            tenant_id="demo",
            worker_id="worker-1",
            source_type="test",
            event_type="demo.event",
        )
    )

    status = store.runtime_status()

    assert status.status.value == "degraded"
    assert status.selected_backend == "file"
    assert status.ground_truth == "file"
    assert status.last_error == "redis down"
