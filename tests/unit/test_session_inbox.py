# edition: baseline
import pytest

from src.autonomy.inbox import InboxItem, SessionInboxStore
from src.common.runtime_status import ComponentStatus


@pytest.mark.asyncio
async def test_inbox_write_fetch_and_consume(tmp_path):
    store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    item = InboxItem(
        tenant_id="demo",
        worker_id="w1",
        source_type="test",
        event_type="test.event",
        dedupe_key="d1",
        payload={"msg": "hello"},
    )

    written = await store.write(item)
    pending = await store.fetch_pending(tenant_id="demo", worker_id="w1")

    assert len(pending) == 1
    assert pending[0].status == "PROCESSING"
    assert pending[0].inbox_id == written.inbox_id

    await store.mark_consumed([written.inbox_id], tenant_id="demo", worker_id="w1")
    consumed = await store.get_by_id(written.inbox_id, tenant_id="demo", worker_id="w1")
    assert consumed is not None
    assert consumed.status == "CONSUMED"


@pytest.mark.asyncio
async def test_inbox_requeues_stale_processing_items(tmp_path):
    store = SessionInboxStore(
        redis_client=None,
        fallback_dir=tmp_path,
        processing_timeout_minutes=1,
    )
    item = InboxItem(
        tenant_id="demo",
        worker_id="w1",
        source_type="test",
        event_type="test.event",
        status="PROCESSING",
        processing_at="2000-01-01T00:00:00+00:00",
    )
    await store.write(item)

    pending = await store.fetch_pending(tenant_id="demo", worker_id="w1")

    assert len(pending) == 1
    assert pending[0].status == "PROCESSING"
    assert pending[0].processing_at != "2000-01-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_inbox_claim_pending_is_atomic(tmp_path):
    store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)
    item = await store.write(
        InboxItem(
            tenant_id="demo",
            worker_id="w1",
            source_type="test",
            event_type="task.confirmation_requested",
        )
    )

    claimed = await store.claim_pending(
        item.inbox_id,
        tenant_id="demo",
        worker_id="w1",
        event_type="task.confirmation_requested",
    )
    second = await store.claim_pending(
        item.inbox_id,
        tenant_id="demo",
        worker_id="w1",
        event_type="task.confirmation_requested",
    )

    assert claimed is not None
    assert claimed.status == "PROCESSING"
    assert second is None


@pytest.mark.asyncio
async def test_list_pending_persists_stale_processing_recovery(tmp_path):
    store = SessionInboxStore(
        redis_client=None,
        fallback_dir=tmp_path,
        processing_timeout_minutes=1,
    )
    item = InboxItem(
        tenant_id="demo",
        worker_id="w1",
        source_type="test",
        event_type="task.confirmation_requested",
        status="PROCESSING",
        processing_at="2000-01-01T00:00:00+00:00",
    )
    await store.write(item)

    pending = await store.list_pending(
        tenant_id="demo",
        worker_id="w1",
        event_type="task.confirmation_requested",
    )

    assert len(pending) == 1
    assert pending[0].status == "PENDING"
    stored = await store.get_by_id(item.inbox_id, tenant_id="demo", worker_id="w1")
    assert stored is not None
    assert stored.status == "PENDING"


def test_inbox_runtime_status_uses_file_backend_when_redis_missing(tmp_path):
    store = SessionInboxStore(redis_client=None, fallback_dir=tmp_path)

    status = store.runtime_status()

    assert status.status == ComponentStatus.READY
    assert status.selected_backend == "file"


@pytest.mark.asyncio
async def test_inbox_runtime_status_marks_degraded_after_redis_failure(tmp_path):
    class _FailingRedis:
        async def hgetall(self, _key):
            raise RuntimeError("redis offline")

    store = SessionInboxStore(redis_client=_FailingRedis(), fallback_dir=tmp_path)

    result = await store.fetch_pending(tenant_id="demo", worker_id="w1")

    assert result == ()
    status = store.runtime_status()
    assert status.status == ComponentStatus.DEGRADED
    assert status.selected_backend == "file"
    assert status.last_error == "redis offline"
