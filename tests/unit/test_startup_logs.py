# edition: baseline
from __future__ import annotations

import importlib
import sys
from types import ModuleType
from types import SimpleNamespace

from fastapi import APIRouter
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.bootstrap as bootstrap_module
from src.bootstrap.context import BootstrapContext
from src.common.runtime_status import ComponentRuntimeStatus, ComponentStatus
from src.worker.registry import build_worker_registry


class _WorkerRouter:
    def __init__(self) -> None:
        self._contact_registries = {}


class _FakeOrchestrator:
    def __init__(self, context: BootstrapContext):
        self._context = context
        self.shutdown_called = False

    async def startup(self) -> BootstrapContext:
        return self._context

    async def shutdown(self) -> None:
        self.shutdown_called = True


def _import_app_module(monkeypatch):
    for module_name in (
        "src.api.routes.worker_routes",
        "src.api.routes.chat_routes",
        "src.api.routes.webhook_routes",
        "src.api.routes.channel_routes",
    ):
        stub = ModuleType(module_name)
        stub.router = APIRouter()
        monkeypatch.setitem(sys.modules, module_name, stub)
    monkeypatch.delitem(sys.modules, "src.api.app", raising=False)
    return importlib.import_module("src.api.app")


def _make_context(*, settings) -> BootstrapContext:
    context = BootstrapContext(settings=settings)
    context.set_state("worker_router", _WorkerRouter())
    context.set_state("worker_registry", build_worker_registry([]))
    context.register_runtime_component(
        "redis",
        lambda: ComponentRuntimeStatus(
            component="redis",
            enabled=False,
            status=ComponentStatus.DISABLED,
            selected_backend="redis",
        ),
    )
    context.register_runtime_component(
        "mysql",
        lambda: ComponentRuntimeStatus(
            component="mysql",
            enabled=False,
            status=ComponentStatus.DISABLED,
            selected_backend="mysql",
        ),
    )
    context.register_runtime_component(
        "openviking",
        lambda: ComponentRuntimeStatus(
            component="openviking",
            enabled=False,
            status=ComponentStatus.DISABLED,
            selected_backend="openviking",
        ),
    )
    context.register_runtime_component(
        "session_store",
        lambda: ComponentRuntimeStatus(
            component="session_store",
            enabled=True,
            status=ComponentStatus.READY,
            selected_backend="file",
            ground_truth="file",
        ),
    )
    context.register_runtime_component(
        "message_dedup",
        lambda: ComponentRuntimeStatus(
            component="message_dedup",
            enabled=True,
            status=ComponentStatus.READY,
            selected_backend="memory",
        ),
    )
    context.register_runtime_component(
        "dead_letter_store",
        lambda: ComponentRuntimeStatus(
            component="dead_letter_store",
            enabled=True,
            status=ComponentStatus.READY,
            selected_backend="file",
        ),
    )
    context.register_runtime_component(
        "main_session_meta",
        lambda: ComponentRuntimeStatus(
            component="main_session_meta",
            enabled=True,
            status=ComponentStatus.READY,
            selected_backend="file",
        ),
    )
    context.register_runtime_component(
        "attention_ledger",
        lambda: ComponentRuntimeStatus(
            component="attention_ledger",
            enabled=True,
            status=ComponentStatus.READY,
            selected_backend="file",
        ),
    )
    return context


def test_lifespan_logs_runtime_summary(monkeypatch):
    app_module = _import_app_module(monkeypatch)
    settings = SimpleNamespace(
        runtime_profile="local",
        redis_enabled=False,
        mysql_enabled=False,
        openviking_enabled=False,
        persona_auto_reload_enabled=False,
    )
    context = _make_context(settings=settings)
    orchestrator = _FakeOrchestrator(context)
    info_logs: list[str] = []

    monkeypatch.setattr(
        bootstrap_module,
        "create_orchestrator",
        lambda tenant_id="demo": orchestrator,
    )
    monkeypatch.setattr(app_module.logger, "info", lambda message, *args: info_logs.append(message % args if args else message))
    monkeypatch.setattr(app_module.logger, "warning", lambda *_args, **_kwargs: None)

    app = FastAPI(lifespan=app_module.lifespan)
    with TestClient(app):
        pass

    assert any(message.startswith("[Runtime] profile=local ") for message in info_logs)
    assert any("session_store=file" in message for message in info_logs)
    assert any("dead_letter_store=file" in message for message in info_logs)
    assert any("main_session_meta=file" in message for message in info_logs)
    assert any("attention_ledger=file" in message for message in info_logs)
    assert orchestrator.shutdown_called is True


def test_lifespan_logs_runtime_profile_override_warning(monkeypatch):
    app_module = _import_app_module(monkeypatch)
    settings = SimpleNamespace(
        runtime_profile="local",
        redis_enabled=True,
        mysql_enabled=False,
        openviking_enabled=False,
        persona_auto_reload_enabled=False,
    )
    context = _make_context(settings=settings)
    orchestrator = _FakeOrchestrator(context)
    warning_logs: list[str] = []

    monkeypatch.setattr(
        bootstrap_module,
        "create_orchestrator",
        lambda tenant_id="demo": orchestrator,
    )
    monkeypatch.setattr(app_module.logger, "info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_module.logger, "warning", lambda message, *args: warning_logs.append(message % args if args else message))

    app = FastAPI(lifespan=app_module.lifespan)
    with TestClient(app):
        pass

    assert "[Runtime] runtime_profile=local override redis=true default=false" in warning_logs
