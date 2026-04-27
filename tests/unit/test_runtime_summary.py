# edition: baseline
from types import SimpleNamespace

from src.common.runtime_status import ComponentRuntimeStatus, ComponentStatus
from src.runtime.runtime_summary import build_runtime_summary


def test_build_runtime_summary_compacts_key_runtime_state():
    app_state = SimpleNamespace(
        runtime_profile="local",
        snapshot_runtime_components=lambda: {
            "redis": ComponentRuntimeStatus(
                component="redis",
                enabled=False,
                status=ComponentStatus.DISABLED,
                selected_backend="redis",
            ),
            "mysql": ComponentRuntimeStatus(
                component="mysql",
                enabled=False,
                status=ComponentStatus.DISABLED,
                selected_backend="mysql",
            ),
            "openviking": ComponentRuntimeStatus(
                component="openviking",
                enabled=False,
                status=ComponentStatus.DISABLED,
                selected_backend="openviking",
            ),
            "session_store": ComponentRuntimeStatus(
                component="session_store",
                enabled=True,
                status=ComponentStatus.READY,
                selected_backend="file",
            ),
            "inbox_store": ComponentRuntimeStatus(
                component="inbox_store",
                enabled=True,
                status=ComponentStatus.READY,
                selected_backend="file",
            ),
            "message_dedup": ComponentRuntimeStatus(
                component="message_dedup",
                enabled=True,
                status=ComponentStatus.READY,
                selected_backend="memory",
            ),
            "dead_letter_store": ComponentRuntimeStatus(
                component="dead_letter_store",
                enabled=True,
                status=ComponentStatus.READY,
                selected_backend="file",
            ),
            "main_session_meta": ComponentRuntimeStatus(
                component="main_session_meta",
                enabled=True,
                status=ComponentStatus.READY,
                selected_backend="file",
            ),
            "attention_ledger": ComponentRuntimeStatus(
                component="attention_ledger",
                enabled=True,
                status=ComponentStatus.READY,
                selected_backend="file",
            ),
        },
        resolve_default_worker=lambda: SimpleNamespace(
            worker_id="analyst-01",
            worker_loaded=True,
        ),
    )

    summary = build_runtime_summary(app_state)

    assert "profile=local" in summary
    assert "default_worker=analyst-01" in summary
    assert "redis=disabled" in summary
    assert "session_store=file" in summary
    assert "message_dedup=memory" in summary
    assert "dead_letter_store=file" in summary
    assert "main_session_meta=file" in summary
    assert "attention_ledger=file" in summary
