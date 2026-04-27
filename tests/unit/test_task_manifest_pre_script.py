# edition: baseline
from src.worker.scripts.models import InlineScript, ScriptRef
from src.worker.task import TaskManifest, TaskStatus, create_task_manifest


def test_task_manifest_roundtrips_inline_pre_script():
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="run task",
        pre_script=InlineScript(
            source="print('hello')",
            enabled_tools=("read_file",),
            timeout_seconds=42,
            max_tool_calls=7,
        ),
    )

    restored = TaskManifest.from_dict(manifest.to_dict())

    assert isinstance(restored.pre_script, InlineScript)
    assert restored.pre_script.source == "print('hello')"
    assert restored.pre_script.enabled_tools == ("read_file",)
    assert restored.pre_script.timeout_seconds == 42
    assert restored.pre_script.max_tool_calls == 7


def test_task_manifest_roundtrips_script_ref():
    manifest = TaskManifest(
        task_id="task-1",
        worker_id="worker-1",
        tenant_id="tenant-1",
        status=TaskStatus.PENDING,
        task_description="run task",
        pre_script=ScriptRef(
            tool_name="fetch_metrics",
            tool_input=(("env", "prod"),),
        ),
    )

    restored = TaskManifest.from_dict(manifest.to_dict())

    assert isinstance(restored.pre_script, ScriptRef)
    assert restored.pre_script.tool_name == "fetch_metrics"
    assert restored.pre_script.input_dict == {"env": "prod"}


def test_task_manifest_accepts_legacy_payload_without_pre_script():
    restored = TaskManifest.from_dict(
        {
            "task_id": "task-1",
            "worker_id": "worker-1",
            "tenant_id": "tenant-1",
            "status": "pending",
            "task_description": "legacy",
        }
    )

    assert restored.pre_script is None
