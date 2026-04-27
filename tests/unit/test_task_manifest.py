# edition: baseline
from src.worker.task import TaskManifest, TaskProvenance, TaskStatus, create_task_manifest


def test_task_manifest_serializes_provenance_and_gate_level():
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="检查周报",
        gate_level="auto",
        provenance=TaskProvenance(
            source_type="goal_task",
            source_id="goal-1",
            goal_id="goal-1",
            goal_task_id="task-1",
            suggestion_id="sugg-1",
        ),
    )

    restored = TaskManifest.from_dict(manifest.to_dict())

    assert restored.gate_level == "auto"
    assert restored.provenance.source_type == "goal_task"
    assert restored.provenance.goal_id == "goal-1"
    assert restored.provenance.goal_task_id == "task-1"
    assert restored.provenance.suggestion_id == "sugg-1"


def test_task_manifest_from_legacy_payload_keeps_defaults():
    restored = TaskManifest.from_dict(
        {
            "task_id": "t-1",
            "worker_id": "worker-1",
            "tenant_id": "tenant-1",
            "status": "completed",
        }
    )

    assert restored.status == TaskStatus.COMPLETED
    assert restored.provenance == TaskProvenance()
    assert restored.gate_level == "gated"
