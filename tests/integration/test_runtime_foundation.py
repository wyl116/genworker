# edition: baseline
from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.health_routes import router as health_router
from src.api.routes.runtime_routes import router as runtime_router
from src.common.runtime_status import ComponentRuntimeStatus, ComponentStatus


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(health_router)
    app.include_router(runtime_router)
    return app


def test_health_stays_healthy_when_readiness_fails(monkeypatch):
    app = _build_app()

    class _Checkpointer:
        async def aget_tuple(self, _config):
            return None

    app.state.worker_registry = SimpleNamespace(count_loaded=lambda: 0)
    app.state.langgraph_checkpointer = _Checkpointer()
    app.state.snapshot_runtime_components = lambda: {
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
    }
    app.state.runtime_component_requirements = {}
    app.state.resolve_default_worker = lambda: SimpleNamespace(
        worker_id="",
        worker_loaded=False,
    )
    app.state.bootstrap_context = SimpleNamespace(llm_ready=True)

    monkeypatch.setattr(
        "src.api.routes.health_routes.get_settings",
        lambda: SimpleNamespace(
            service_name="genworker",
            service_version="0.1.0",
            environment="test",
            runtime_profile="local",
            redis_enabled=False,
            mysql_enabled=False,
            openviking_enabled=False,
        ),
    )
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

    client = TestClient(app)

    health = client.get("/health")
    readiness = client.get("/readiness")

    assert health.status_code == 200
    assert health.json()["status"] == "healthy"
    assert readiness.status_code == 200
    assert readiness.json()["status"] == "failed"
    assert "worker_not_loaded" in readiness.json()["blocking_reasons"]


def test_runtime_debug_returns_schema_for_status_card(monkeypatch):
    app = _build_app()

    class _Checkpointer:
        async def aget_tuple(self, _config):
            return None

    app.state.worker_registry = SimpleNamespace(count_loaded=lambda: 1)
    app.state.langgraph_checkpointer = _Checkpointer()
    app.state.snapshot_runtime_components = lambda: {
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
            ground_truth="file",
        ),
        "message_dedup": ComponentRuntimeStatus(
            component="message_dedup",
            enabled=True,
            status=ComponentStatus.READY,
            selected_backend="memory",
            primary_backend="redis",
            fallback_backend="memory",
            ground_truth="memory",
        ),
    }
    app.state.runtime_component_requirements = {}
    app.state.resolve_default_worker = lambda: SimpleNamespace(
        worker_id="analyst-01",
        worker_loaded=True,
    )
    app.state.bootstrap_context = SimpleNamespace(
        llm_ready=True,
        get_state=lambda key, default=None: "demo" if key == "tenant_id" else default,
    )

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

    client = TestClient(app)
    response = client.get("/api/v1/debug/runtime")

    assert response.status_code == 200
    payload = response.json()
    assert payload["runtime_profile"] == "local"
    assert payload["default_worker_id"] == "analyst-01"
    assert payload["dependencies"]["redis"] == "disabled"
    assert payload["components"]["session_store"]["selected_backend"] == "file"


def test_runtime_debug_requires_auth_when_configured(monkeypatch):
    app = _build_app()

    class _Checkpointer:
        async def aget_tuple(self, _config):
            return None

    app.state.worker_registry = SimpleNamespace(count_loaded=lambda: 1)
    app.state.langgraph_checkpointer = _Checkpointer()
    app.state.snapshot_runtime_components = lambda: {}
    app.state.runtime_component_requirements = {}
    app.state.resolve_default_worker = lambda: SimpleNamespace(
        worker_id="analyst-01",
        worker_loaded=True,
    )
    app.state.bootstrap_context = SimpleNamespace(
        llm_ready=True,
        get_state=lambda key, default=None: "demo" if key == "tenant_id" else default,
    )

    monkeypatch.setattr(
        "src.api.auth.get_settings",
        lambda: SimpleNamespace(
            api_bearer_token="secret-token",
            api_key="",
            api_worker_scope="*",
        ),
    )
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

    client = TestClient(app)
    response = client.get("/api/v1/debug/runtime")

    assert response.status_code == 401
