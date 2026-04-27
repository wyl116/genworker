# edition: baseline
from pathlib import Path

import pytest

from src.engine.langgraph.checkpointer import LangGraphCheckpointer


@pytest.mark.asyncio
async def test_langgraph_checkpointer_roundtrip(tmp_path: Path):
    checkpointer = LangGraphCheckpointer(tmp_path)
    await checkpointer.register_thread(
        thread_id="thread-1",
        tenant_id="tenant-1",
        worker_id="worker-1",
        skill_id="skill-1",
        source_path="/tmp/skill.md",
    )

    config = {
        "configurable": {
            "thread_id": "thread-1",
            "tenant_id": "tenant-1",
            "worker_id": "worker-1",
            "skill_id": "skill-1",
            "source_path": "/tmp/skill.md",
            "state_whitelist": ["task"],
        }
    }
    checkpoint = {
        "id": "cp-1",
        "channel_values": {"task": "hello"},
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": [],
        "v": 1,
        "ts": "now",
    }
    await checkpointer.aput(config, checkpoint, {"step": 1}, {})

    loaded = await checkpointer.aget_tuple({"configurable": {"thread_id": "thread-1"}})

    assert loaded is not None
    assert loaded.checkpoint["id"] == "cp-1"
    record = await checkpointer.load_by_thread("thread-1")
    assert record is not None
    assert record.skill_id == "skill-1"
    assert record.source_path == "/tmp/skill.md"
