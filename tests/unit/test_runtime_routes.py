# edition: baseline
from types import SimpleNamespace

import pytest

from src.api.routes.health_routes import readiness_check
from src.api.routes.runtime_routes import runtime_debug
from src.common.runtime_status import ComponentRuntimeStatus, ComponentStatus


class _State:
    def __init__(self) -> None:
        self.worker_registry = SimpleNamespace(count_loaded=lambda: 1)
        self.langgraph_checkpointer = SimpleNamespace(
            aget_tuple=lambda _config: None
        )
        self.snapshot_runtime_components = lambda: {
            "redis": ComponentRuntimeStatus(
                component="redis",
                enabled=False,
                status=ComponentStatus.DISABLED,
                selected_backend="redis",
            ),
            "session_store": ComponentRuntimeStatus(
                component="session_store",
                enabled=True,
                status=ComponentStatus.READY,
                selected_backend="file",
                ground_truth="file",
            ),
        }
        self.runtime_component_requirements = {}
        self.resolve_default_worker = lambda: SimpleNamespace(
            worker_id="analyst-01",
            worker_loaded=True,
        )
        self.bootstrap_context = SimpleNamespace(
            llm_ready=True,
            get_state=lambda key, default=None: "demo" if key == "tenant_id" else default,
        )


@pytest.mark.asyncio
async def test_readiness_check_reports_dependency_statuses(monkeypatch):
    monkeypatch.setattr(
        "src.api.routes.health_routes.get_settings",
        lambda: SimpleNamespace(
            runtime_profile="local",
            service_name="genworker",
            service_version="0.1.0",
            environment="test",
            redis_enabled=False,
            mysql_enabled=False,
            openviking_enabled=False,
        ),
    )
    monkeypatch.setattr(
        "src.api.routes.health_routes.importlib.import_module",
        lambda _name: object(),
    )

    async def _aget_tuple(_config):
        return None

    state = _State()
    state.langgraph_checkpointer = SimpleNamespace(aget_tuple=_aget_tuple)
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    payload = await readiness_check(request)

    assert payload["status"] == "ready"
    assert payload["dependencies"]["redis"] == "disabled"
    assert payload["dependencies"]["langgraph"] == "ready"


@pytest.mark.asyncio
async def test_runtime_debug_includes_default_worker_and_components(monkeypatch):
    monkeypatch.setattr(
        "src.api.routes.runtime_routes.get_settings",
        lambda: SimpleNamespace(
            runtime_profile="local",
            redis_enabled=False,
            mysql_enabled=False,
            openviking_enabled=False,
        ),
    )
    monkeypatch.setattr(
        "src.api.routes.health_routes.importlib.import_module",
        lambda _name: object(),
    )

    async def _aget_tuple(_config):
        return None

    state = _State()
    state.langgraph_checkpointer = SimpleNamespace(aget_tuple=_aget_tuple)
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    payload = await runtime_debug(request)

    assert payload["default_worker_id"] == "analyst-01"
    assert payload["components"]["session_store"]["selected_backend"] == "file"
    assert payload["runtime_profile"] == "local"
