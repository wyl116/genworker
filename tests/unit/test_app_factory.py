# edition: baseline
from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.api.app import create_app


def test_create_app_skips_im_config_routes_when_im_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.api.app.get_settings",
        lambda: SimpleNamespace(
            service_name="genworker",
            service_version="0.1.0",
            im_channel_enabled=False,
        ),
    )
    app = create_app()
    client = TestClient(app)

    response = client.get("/api/v1/workers/analyst-01/im-config")

    assert response.status_code == 404


def test_create_app_registers_im_config_routes_when_im_enabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.api.app.get_settings",
        lambda: SimpleNamespace(
            service_name="genworker",
            service_version="0.1.0",
            im_channel_enabled=True,
        ),
    )
    app = create_app()

    paths = {route.path for route in app.routes}

    assert "/api/v1/workers/{worker_id}/im-config" in paths


def test_create_app_tolerates_missing_im_channel_enabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.api.app.get_settings",
        lambda: SimpleNamespace(
            service_name="genworker",
            service_version="0.1.0",
        ),
    )

    app = create_app()

    paths = {route.path for route in app.routes}

    assert "/api/v1/workers/{worker_id}/im-config" not in paths
