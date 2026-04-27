# edition: baseline
from pathlib import Path

import pytest

from src.autonomy.inbox import SessionInboxStore
from src.engine.langgraph.interrupt_bridge import InterruptBridge
from src.skills.models import NodeDefinition, NodeKind


@pytest.mark.asyncio
async def test_interrupt_bridge_writes_langgraph_inbox_contract(tmp_path: Path):
    store = SessionInboxStore(fallback_dir=tmp_path)
    bridge = InterruptBridge(inbox_store=store)
    inbox_id, prompt = await bridge.create_inbox(
        tenant_id="tenant-1",
        worker_id="worker-1",
        thread_id="thread-1",
        skill_id="skill-1",
        node=NodeDefinition(
            name="human_approval",
            kind=NodeKind.INTERRUPT,
            prompt_ref="approval_prompt",
            inbox_event_type="order_approval",
        ),
        state={"order_id": "o-1", "amount": 99.0},
        state_whitelist=("order_id", "amount"),
        prompt_template="订单 {order_id} 金额 {amount}，请回复 /approve_confirmation {inbox_id}",
    )

    item = await store.get_by_id(inbox_id, tenant_id="tenant-1", worker_id="worker-1")

    assert item is not None
    assert item.source_type == "langgraph"
    assert item.event_type == "order_approval"
    assert item.payload["engine"] == "langgraph"
    assert item.payload["thread_id"] == "thread-1"
    assert item.payload["skill_id"] == "skill-1"
    assert item.payload["node"] == "human_approval"
    assert item.payload["state_digest"]
    assert item.payload["prompt"] == prompt
    assert inbox_id in prompt
