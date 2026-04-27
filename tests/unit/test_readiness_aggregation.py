# edition: baseline
from src.common.runtime_status import ComponentRuntimeStatus, ComponentStatus
from src.runtime.runtime_views import aggregate_readiness


def test_aggregate_readiness_fails_when_required_component_failed():
    payload = aggregate_readiness(
        runtime_profile="local",
        worker_loaded=True,
        model_ready=True,
        components={
            "redis": ComponentRuntimeStatus(
                component="redis",
                enabled=True,
                status=ComponentStatus.FAILED,
                selected_backend="redis",
                last_error="connection refused",
            )
        },
        requirements={"redis": True},
        dependencies={"redis": "failed", "mysql": "disabled", "openviking": "disabled", "langgraph": "ready"},
        langgraph_probe={"import_ok": True, "checkpointer_ok": True},
    )

    assert payload["status"] == "failed"
    assert "redis failed: connection refused" in payload["blocking_reasons"]


def test_aggregate_readiness_degrades_when_optional_component_falls_back():
    payload = aggregate_readiness(
        runtime_profile="local",
        worker_loaded=True,
        model_ready=True,
        components={
            "message_dedup": ComponentRuntimeStatus(
                component="message_dedup",
                enabled=True,
                status=ComponentStatus.DEGRADED,
                selected_backend="memory",
                primary_backend="redis",
                fallback_backend="memory",
                last_error="redis down",
            )
        },
        requirements={},
        dependencies={"redis": "ready", "mysql": "disabled", "openviking": "disabled", "langgraph": "ready"},
        langgraph_probe={"import_ok": True, "checkpointer_ok": True},
    )

    assert payload["status"] == "degraded"
    assert "message_dedup fell back to memory: redis down" in payload["warnings"]


def test_aggregate_readiness_blocks_on_worker_and_model():
    payload = aggregate_readiness(
        runtime_profile="local",
        worker_loaded=False,
        model_ready=False,
        components={},
        requirements={},
        dependencies={"redis": "disabled", "mysql": "disabled", "openviking": "disabled", "langgraph": "ready"},
        langgraph_probe={"import_ok": True, "checkpointer_ok": True},
    )

    assert payload["status"] == "failed"
    assert payload["blocking_reasons"] == ["worker_not_loaded", "model_not_ready"]
