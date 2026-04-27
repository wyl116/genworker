# edition: baseline
from src.common.runtime_status import (
    ComponentRuntimeStatus,
    ComponentStatus,
    aggregate_component_statuses,
)


def test_component_runtime_status_to_public_dict_sanitizes_last_error():
    payload = ComponentRuntimeStatus(
        component="message_dedup",
        enabled=True,
        status=ComponentStatus.DEGRADED,
        selected_backend="memory",
        primary_backend="redis",
        fallback_backend="memory",
        last_error="first line\nsecret stack trace\nmore details",
    ).to_public_dict()

    assert payload["status"] == "degraded"
    assert payload["last_error"] == "first line"


def test_component_status_serializes_to_value():
    assert ComponentStatus.READY.value == "ready"
    assert str(ComponentStatus.FAILED) == "ComponentStatus.FAILED"


def test_aggregate_component_statuses_merges_degraded_instances():
    payload = aggregate_component_statuses(
        "attention_ledger",
        (
            ComponentRuntimeStatus(
                component="attention_ledger",
                enabled=True,
                status=ComponentStatus.READY,
                selected_backend="redis",
                primary_backend="redis",
                fallback_backend="file",
            ),
            ComponentRuntimeStatus(
                component="attention_ledger",
                enabled=True,
                status=ComponentStatus.DEGRADED,
                selected_backend="file",
                primary_backend="redis",
                fallback_backend="file",
                last_error="redis timeout",
            ),
        ),
    )

    assert payload.component == "attention_ledger"
    assert payload.status == ComponentStatus.DEGRADED
    assert payload.selected_backend == "mixed"
    assert payload.last_error == "redis timeout"
