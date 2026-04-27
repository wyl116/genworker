# edition: baseline
from types import SimpleNamespace

from fastapi import FastAPI
import pytest

from src.runtime.app_state import store_dependencies as _store_dependencies
from src.api.routes.health_routes import health_check
from src.bootstrap.context import BootstrapContext
from src.common.runtime_status import ComponentRuntimeStatus, ComponentStatus
from src.worker.registry import build_worker_registry


class _WorkerRouterWithSetters:
    def __init__(self) -> None:
        self._contact_registries = {}
        self.session_search_index = None
        self.task_spawner = None

    def set_session_search_index(self, search_index) -> None:
        self.session_search_index = search_index

    def set_task_spawner(self, task_spawner) -> None:
        self.task_spawner = task_spawner


def test_store_dependencies_exposes_runtime_helpers_on_app_state(tmp_path):
    context = BootstrapContext(settings=SimpleNamespace())
    worker_router = _WorkerRouterWithSetters()
    session_inbox_store = object()
    goal_inbox_store = object()
    integration_inbox_store = object()
    isolated_run_manager = object()
    main_session_runtimes = {"w1": object()}
    goal_lock_registry = object()
    lifecycle_services = object()
    session_search_index = object()
    task_spawner = object()

    context.set_state("workspace_root", tmp_path)
    context.set_state("worker_router", worker_router)
    context.set_state("worker_registry", build_worker_registry([]))
    context.set_state("trigger_managers", {})
    context.set_state("worker_schedulers", {})
    context.set_state("heartbeat_runners", {})
    context.set_state("main_session_runtimes", main_session_runtimes)
    context.set_state("sensor_registries", {})
    context.set_state("isolated_run_manager", isolated_run_manager)
    context.set_state("session_inbox_store", session_inbox_store)
    context.set_state("goal_inbox_store", goal_inbox_store)
    context.set_state("integration_inbox_store", integration_inbox_store)
    context.set_state("goal_lock_registry", goal_lock_registry)
    context.set_state("lifecycle_services", lifecycle_services)
    context.set_state("session_search_index", session_search_index)
    context.set_state("task_spawner", task_spawner)
    context.set_state("langgraph_checkpointer", object())
    context.set_state("langgraph_engine", object())
    context.register_runtime_component(
        "redis",
        lambda: ComponentRuntimeStatus(
            component="redis",
            enabled=False,
            status=ComponentStatus.DISABLED,
            selected_backend="redis",
        ),
    )

    app = FastAPI()
    _store_dependencies(app, context)

    assert app.state.main_session_runtimes is main_session_runtimes
    assert app.state.isolated_run_manager is isolated_run_manager
    assert app.state.session_inbox_store is session_inbox_store
    assert app.state.goal_inbox_store is goal_inbox_store
    assert app.state.integration_inbox_store is integration_inbox_store
    assert app.state.goal_lock_registry is goal_lock_registry
    assert app.state.lifecycle_services is lifecycle_services
    assert app.state.session_search_index is session_search_index
    assert app.state.task_spawner is task_spawner
    assert app.state.langgraph_checkpointer is not None
    assert app.state.langgraph_engine is not None
    assert callable(app.state.snapshot_runtime_components)
    assert callable(app.state.resolve_default_worker)
    assert app.state.snapshot_runtime_components()["redis"].status == ComponentStatus.DISABLED
    assert worker_router.session_search_index is session_search_index
    assert worker_router.task_spawner is task_spawner


def test_store_dependencies_exposes_engine_registry(tmp_path):
    context = BootstrapContext(settings=SimpleNamespace())
    worker_router = _WorkerRouterWithSetters()
    engine_registry = {
        "autonomous": {"ready": True},
        "deterministic": {"ready": True},
        "hybrid": {"ready": True},
        "planning": {"ready": True},
        "langgraph": {"import_ok": True, "checkpointer_ok": True},
    }

    context.set_state("workspace_root", tmp_path)
    context.set_state("worker_router", worker_router)
    context.set_state("worker_registry", object())
    context.set_state("trigger_managers", {})
    context.set_state("worker_schedulers", {})
    context.set_state("heartbeat_runners", {})
    context.set_state("sensor_registries", {})
    context.set_state("engine_registry", engine_registry)

    app = FastAPI()
    _store_dependencies(app, context)

    assert app.state.engine_registry == engine_registry


@pytest.mark.asyncio
async def test_health_check_returns_engine_registry():
    class _Checkpointer:
        async def aget_tuple(self, config):
            assert config["configurable"]["thread_id"] == "__healthcheck__"
            return None

    class _State:
        engine_registry = {
            "autonomous": {"ready": True},
            "planning": {"ready": True},
            "langgraph": {"import_ok": True, "checkpointer_ok": True},
        }
        langgraph_checkpointer = _Checkpointer()

    request = SimpleNamespace(app=SimpleNamespace(state=_State()))

    from unittest.mock import patch

    with patch("src.api.routes.health_routes.importlib.import_module", return_value=object()):
        payload = await health_check(request)

    assert payload["status"] == "healthy"
    assert payload["engines"]["autonomous"]["ready"] is True
    assert payload["engines"]["planning"]["ready"] is True
    assert payload["engines"]["langgraph"]["import_ok"] is True
    assert payload["engines"]["langgraph"]["checkpointer_ok"] is True


@pytest.mark.asyncio
async def test_health_check_reports_failed_langgraph_probe(monkeypatch):
    class _Checkpointer:
        async def aget_tuple(self, config):
            raise RuntimeError("boom")

    class _State:
        engine_registry = {}
        langgraph_checkpointer = _Checkpointer()

    def _raise_import_error(_name: str):
        raise ImportError("missing")

    monkeypatch.setattr("src.api.routes.health_routes.importlib.import_module", _raise_import_error)
    request = SimpleNamespace(app=SimpleNamespace(state=_State()))

    payload = await health_check(request)

    assert payload["engines"]["langgraph"]["import_ok"] is False
    assert payload["engines"]["langgraph"]["checkpointer_ok"] is False
