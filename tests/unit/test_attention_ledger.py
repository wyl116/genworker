# edition: baseline
import pytest

from src.worker.heartbeat.ledger import AttentionLedger


class _FailingRedis:
    async def hset(self, *args, **kwargs):
        raise RuntimeError("redis down")

    async def expire(self, *args, **kwargs):
        raise RuntimeError("redis down")

    async def hget(self, *args, **kwargs):
        raise RuntimeError("redis down")


@pytest.mark.asyncio
async def test_attention_ledger_dedup(tmp_path):
    ledger = AttentionLedger(
        tenant_id="demo",
        worker_id="w1",
        redis_client=None,
        workspace_root=tmp_path,
    )

    await ledger.record_notification("risk:1", "summary")

    assert await ledger.has_notified("risk:1", window_hours=24) is True
    assert await ledger.has_notified("risk:2", window_hours=24) is False


@pytest.mark.asyncio
async def test_attention_ledger_runtime_status_degrades_on_redis_fallback(tmp_path):
    ledger = AttentionLedger(
        tenant_id="demo",
        worker_id="w1",
        redis_client=_FailingRedis(),
        workspace_root=tmp_path,
    )

    await ledger.record_notification("risk:1", "summary")

    status = ledger.runtime_status()
    assert status.status.value == "degraded"
    assert status.selected_backend == "file"
