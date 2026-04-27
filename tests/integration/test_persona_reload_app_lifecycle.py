# edition: baseline
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import src.api.app as app_module
import src.bootstrap as bootstrap_module
from src.bootstrap.context import BootstrapContext
from src.worker.loader import load_worker_entry
from src.worker.registry import build_worker_registry


def _write_persona(
    path: Path,
    *,
    name: str,
    goal_task_actions: list[str] | None = None,
) -> None:
    actions_block = ""
    if goal_task_actions is not None:
        actions_yaml = "\n".join(f"    - {action}" for action in goal_task_actions)
        actions_block = (
            "heartbeat:\n"
            "  goal_task_actions:\n"
            f"{actions_yaml}\n"
        )
    path.write_text(
        (
            "---\n"
            "identity:\n"
            f"  worker_id: analyst-01\n"
            f"  name: {name}\n"
            "  role: analyst\n"
            "default_skill: general-query\n"
            f"{actions_block}"
            "---\n"
            "Analyst instructions.\n"
        ),
        encoding="utf-8",
    )


class _FakeOrchestrator:
    def __init__(self, context: BootstrapContext):
        self._context = context
        self.shutdown_called = False

    async def startup(self) -> BootstrapContext:
        return self._context

    async def shutdown(self) -> None:
        self.shutdown_called = True


def _wait_until(predicate, timeout_seconds: float = 2.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition was not met before timeout")


def test_app_lifespan_hot_reloads_persona_locally(
    tmp_path: Path,
    monkeypatch,
):
    workspace_root = tmp_path / "workspace"
    worker_dir = workspace_root / "tenants" / "demo" / "workers" / "analyst-01"
    (workspace_root / "system" / "skills").mkdir(parents=True, exist_ok=True)
    (workspace_root / "tenants" / "demo" / "skills").mkdir(parents=True, exist_ok=True)
    (worker_dir / "skills").mkdir(parents=True, exist_ok=True)

    persona_path = worker_dir / "PERSONA.md"
    _write_persona(persona_path, name="Analyst One", goal_task_actions=["escalate"])

    entry = load_worker_entry(
        workspace_root=workspace_root,
        tenant_id="demo",
        worker_id="analyst-01",
    )
    registry = build_worker_registry([entry], default_worker_id="analyst-01")

    settings = SimpleNamespace(
        service_name="genworker-test",
        service_version="test",
        persona_auto_reload_enabled=True,
        persona_auto_reload_interval_seconds=0.05,
        persona_auto_reload_debounce_seconds=0.01,
        heartbeat_goal_task_actions="escalate,recover,investigate",
        heartbeat_goal_isolated_actions="replan,deep_review",
        heartbeat_goal_isolated_deviation_threshold=0.9,
    )

    context = BootstrapContext(settings=settings)
    context.set_state("workspace_root", workspace_root)
    context.set_state("worker_registry", registry)
    context.set_state("worker_router", SimpleNamespace(_worker_registry=registry))

    orchestrator = _FakeOrchestrator(context)
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        bootstrap_module,
        "create_orchestrator",
        lambda tenant_id="demo": orchestrator,
    )

    app = app_module.create_app()
    with TestClient(app) as client:
        watcher = app.state.persona_reload_watcher
        _wait_until(
            lambda: watcher.operational_snapshot["tracked_workers"] == 1,
            timeout_seconds=2.0,
        )

        overview = client.get(
            "/api/v1/worker/ops/overview",
            params={"tenant_id": "demo"},
        ).json()
        assert overview["persona_reload"]["configured"] is True
        assert overview["persona_reload"]["running"] is True
        assert overview["persona_reload"]["tracked_workers"] == 1

        _write_persona(
            persona_path,
            name="Analyst Updated",
            goal_task_actions=["recover"],
        )

        _wait_until(
            lambda: (
                app.state.worker_registry.get("analyst-01") is not None
                and app.state.worker_registry.get("analyst-01").worker.name
                == "Analyst Updated"
            ),
            timeout_seconds=2.0,
        )

        updated_entry = app.state.worker_registry.get("analyst-01")
        assert updated_entry is not None
        assert updated_entry.worker.heartbeat_config.goal_task_actions == ("recover",)

        overview = client.get(
            "/api/v1/worker/ops/overview",
            params={"tenant_id": "demo"},
        ).json()
        assert overview["persona_reload"]["reload_count"] >= 1
        assert any(
            item["worker_id"] == "analyst-01"
            for item in overview["persona_reload"]["recent_reloads"]
        )

    assert watcher.operational_snapshot["running"] is False
    assert orchestrator.shutdown_called is True
