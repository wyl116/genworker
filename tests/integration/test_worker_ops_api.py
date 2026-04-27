# edition: baseline
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.worker_routes import router as worker_router
from src.events.bus import EventBus
from src.skills.registry import SkillRegistry
from src.worker.duty.trigger_manager import TriggerManager
from src.worker.models import Worker, WorkerIdentity
from src.worker.registry import WorkerEntry, build_worker_registry
from src.worker.scheduler import SchedulerConfig, WorkerScheduler


def _make_app(tmp_path: Path) -> FastAPI:
    app = FastAPI()
    app.include_router(worker_router)

    worker = Worker(
        identity=WorkerIdentity(
            worker_id="analyst-01",
            name="Analyst One",
            role="analyst",
        ),
        default_skill="general-query",
    )
    worker_registry = build_worker_registry(
        entries=[WorkerEntry(worker=worker, skill_registry=SkillRegistry.from_skills([]))],
        default_worker_id="analyst-01",
    )

    worker_dir = tmp_path / "tenants" / "demo" / "workers" / "analyst-01"
    (worker_dir / "duties").mkdir(parents=True, exist_ok=True)
    (worker_dir / "goals").mkdir(parents=True, exist_ok=True)
    (worker_dir / "tasks" / "active").mkdir(parents=True, exist_ok=True)
    (worker_dir / "PERSONA.md").write_text(
        (
            "---\n"
            "identity:\n"
            "  worker_id: analyst-01\n"
            "  name: Analyst One\n"
            "---\n"
            "\n"
            "# Analyst One\n"
        ),
        encoding="utf-8",
    )
    (worker_dir / "duties" / "d1.md").write_text("---\n---\n", encoding="utf-8")
    (worker_dir / "goals" / "g1.md").write_text("---\n---\n", encoding="utf-8")
    (worker_dir / "tasks" / "active" / "t1.json").write_text("{}", encoding="utf-8")

    scheduler = WorkerScheduler(SchedulerConfig(max_concurrent_tasks=3, daily_task_quota=20))
    scheduler._daily_count = 2

    trigger_manager = TriggerManager(
        scheduler=MagicMock(),
        event_bus=EventBus(),
        duty_executor=AsyncMock(),
    )
    trigger_manager._registrations = {
        "daily-quality-watch": [
            ("schedule", "duty:daily-quality-watch:trigger:morning"),
            ("condition", "duty:daily-quality-watch:condition:anomaly"),
        ],
    }

    app.state.worker_router = AsyncMock()
    app.state.worker_registry = worker_registry
    app.state.trigger_managers = {"analyst-01": trigger_manager}
    app.state.worker_schedulers = {"analyst-01": scheduler}
    app.state.heartbeat_runners = {}
    app.state.sensor_registries = {
        "analyst-01": SimpleNamespace(
            health={
                "sensor_count": 1,
                "sensors": {
                    "email": {
                        "delivery_mode": "poll",
                        "config": {"poll_interval": "15m"},
                    },
                },
            }
        )
    }
    app.state.worker_reload_status = {
        ("demo", "analyst-01"): {
            "tenant_id": "demo",
            "worker_id": "analyst-01",
            "trigger_source": "auto",
            "changed_files": ["goals/g1.md"],
            "reloaded_at": "2026-04-07T00:01:00+00:00",
        },
    }
    app.state.workspace_root = str(tmp_path)
    app.state.persona_reload_watcher = SimpleNamespace(
        operational_snapshot={
            "configured": True,
            "running": True,
            "interval_seconds": 2.0,
            "debounce_seconds": 1.0,
            "tracked_workers": 1,
            "tracked_files": 3,
            "reload_count": 3,
            "last_scan_completed_at": "2026-04-07T00:00:00+00:00",
            "last_error": "",
            "recent_reloads": [
                {
                    "tenant_id": "demo",
                    "worker_id": "analyst-01",
                    "changed_files": ["PERSONA.md"],
                    "reloaded_at": "2026-04-07T00:00:00+00:00",
                },
            ],
        }
    )
    async def _reload_worker_runtime(worker_id: str, tenant_id: str):
        return {
            "worker_id": worker_id,
            "name": "Analyst One",
            "heartbeat_config": {
                "goal_task_actions": ["escalate"],
                "goal_isolated_actions": ["replan"],
                "goal_isolated_deviation_threshold": 0.9,
            },
            "heartbeat_runner_refreshed": False,
            "reload_metadata": {
                "tenant_id": tenant_id,
                "worker_id": worker_id,
                "trigger_source": "manual",
                "changed_files": [],
                "reloaded_at": "2026-04-07T00:02:00+00:00",
            },
        }
    app.state.reload_worker_runtime = _reload_worker_runtime
    return app


class TestWorkerOpsApi:
    def test_ops_overview_returns_worker_backend_state(self, tmp_path: Path):
        app = _make_app(tmp_path)
        client = TestClient(app)

        response = client.get("/api/v1/worker/ops/overview", params={"tenant_id": "demo"})

        assert response.status_code == 200
        data = response.json()
        assert data["tenant_id"] == "demo"
        assert data["worker_count"] == 1
        assert data["persona_reload"]["running"] is True
        assert data["persona_reload"]["reload_count"] == 3
        worker = data["workers"][0]
        assert worker["worker_id"] == "analyst-01"
        assert worker["backend_online"] is True
        assert worker["scheduler"]["registered"] is True
        assert worker["scheduler"]["daily_count"] == 2
        assert worker["triggers"]["resource_count"] == 2
        assert worker["sensors"]["sensor_count"] == 1
        assert worker["autonomous_capabilities"]["sensing"] is True
        assert worker["reload_status"]["trigger_source"] == "auto"
        assert worker["reload_status"]["changed_files"] == ["goals/g1.md"]
        assert worker["runtime"]["duties_count"] == 1
        assert worker["runtime"]["goals_count"] == 1
        assert worker["runtime"]["active_task_count"] == 1

    def test_ops_overview_returns_empty_without_registry(self):
        app = FastAPI()
        app.include_router(worker_router)
        client = TestClient(app)

        response = client.get("/api/v1/worker/ops/overview")

        assert response.status_code == 200
        assert response.json() == {
            "tenant_id": "demo",
            "worker_count": 0,
            "workers": [],
            "persona_reload": {
                "configured": False,
                "running": False,
                "interval_seconds": 2.0,
                "debounce_seconds": 1.0,
                "tracked_workers": 0,
                "tracked_files": 0,
                "reload_count": 0,
                "last_scan_completed_at": None,
                "last_error": "",
                "recent_reloads": [],
            },
        }

    def test_ops_reload_returns_runtime_refresh_payload(self, tmp_path: Path):
        app = _make_app(tmp_path)
        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/ops/reload",
            params={"tenant_id": "demo", "worker_id": "analyst-01"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "reloaded"
        assert data["worker_id"] == "analyst-01"
        assert data["heartbeat_config"]["goal_task_actions"] == ["escalate"]
        assert data["reload_metadata"]["trigger_source"] == "manual"

    def test_ops_config_returns_persona_duties_and_goals(self, tmp_path: Path):
        app = _make_app(tmp_path)
        client = TestClient(app)

        response = client.get(
            "/api/v1/worker/ops/config",
            params={"tenant_id": "demo", "worker_id": "analyst-01"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["worker_id"] == "analyst-01"
        assert data["persona"]["filename"] == "PERSONA.md"
        assert "identity:" in data["persona"]["content"]
        assert data["duties"][0]["filename"] == "d1.md"
        assert data["goals"][0]["filename"] == "g1.md"
        assert data["credentials"]["exists"] is False

    def test_ops_config_returns_404_when_persona_missing(self, tmp_path: Path):
        app = _make_app(tmp_path)
        worker_dir = tmp_path / "tenants" / "demo" / "workers" / "analyst-01"
        (worker_dir / "PERSONA.md").unlink()
        client = TestClient(app)

        response = client.get(
            "/api/v1/worker/ops/config",
            params={"tenant_id": "demo", "worker_id": "analyst-01"},
        )

        assert response.status_code == 404
        assert "PERSONA.md not found" in response.json()["detail"]

    def test_ops_overview_requires_auth_when_configured(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        app = _make_app(tmp_path)
        monkeypatch.setattr(
            "src.api.auth.get_settings",
            lambda: SimpleNamespace(
                api_bearer_token="secret-token",
                api_key="",
                api_worker_scope="*",
            ),
        )
        client = TestClient(app)

        response = client.get("/api/v1/worker/ops/overview", params={"tenant_id": "demo"})

        assert response.status_code == 401

    def test_ops_reload_enforces_worker_scope(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        app = _make_app(tmp_path)
        monkeypatch.setattr(
            "src.api.auth.get_settings",
            lambda: SimpleNamespace(
                api_bearer_token="secret-token",
                api_key="",
                api_worker_scope="other-worker",
            ),
        )
        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/ops/reload",
            headers={"Authorization": "Bearer secret-token"},
            params={"tenant_id": "demo", "worker_id": "analyst-01"},
        )

        assert response.status_code == 403
