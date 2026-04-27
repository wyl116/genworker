# edition: baseline
from __future__ import annotations

from src.engine.serializer import deserialize_worker_context, serialize_worker_context
from src.engine.state import WorkerContext
from src.worker.scripts.models import InlineScript


def test_worker_context_serialization_roundtrips_goal_default_pre_script():
    ctx = WorkerContext(
        worker_id="worker-1",
        tenant_id="tenant-1",
        goal_default_pre_script=InlineScript(
            source="print('goal context')",
            enabled_tools=("read_file",),
            timeout_seconds=42,
            max_tool_calls=3,
        ),
    )

    restored = deserialize_worker_context(serialize_worker_context(ctx))

    assert restored.worker_id == "worker-1"
    assert restored.tenant_id == "tenant-1"
    assert isinstance(restored.goal_default_pre_script, InlineScript)
    assert restored.goal_default_pre_script.source == "print('goal context')"
    assert restored.goal_default_pre_script.enabled_tools == ("read_file",)
    assert restored.goal_default_pre_script.timeout_seconds == 42
    assert restored.goal_default_pre_script.max_tool_calls == 3
