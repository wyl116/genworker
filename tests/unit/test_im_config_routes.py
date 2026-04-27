# edition: baseline
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.im_config_routes import router as im_config_router


def _build_app(tmp_path: Path) -> FastAPI:
    worker_dir = tmp_path / "tenants" / "demo" / "workers" / "analyst-01"
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "PERSONA.md").write_text(
        (
            "---\n"
            "identity:\n"
            "  worker_id: analyst-01\n"
            "channels:\n"
            "  - type: slack\n"
            "    connection_mode: socket_mode\n"
            "    chat_ids:\n"
            "      - C123\n"
            "    reply_mode: streaming\n"
            "---\n"
            "Body.\n"
        ),
        encoding="utf-8",
    )
    (worker_dir / "CHANNEL_CREDENTIALS.json").write_text(
        json.dumps(
            {
                "slack": {
                    "bot_token": "xoxb-secret",
                    "app_token": "xapp-secret",
                    "signing_secret": "signing-secret",
                }
            }
        ),
        encoding="utf-8",
    )
    app = FastAPI()
    app.include_router(im_config_router)
    app.state.workspace_root = str(tmp_path)
    app.state.bootstrap_context = object()
    app.state.worker_router = object()
    app.state.im_channel_registry = SimpleNamespace(
        get_adapter=lambda adapter_id: SimpleNamespace(
            _last_error="",
            status_snapshot=lambda: {"last_error": ""},
            health_check=AsyncMock(return_value=True),
        )
        if adapter_id == "slack:demo:analyst-01"
        else None
    )
    return app


def test_get_im_config_masks_secret_fields(tmp_path: Path, monkeypatch) -> None:
    app = _build_app(tmp_path)
    monkeypatch.setattr(
        "src.api.auth.get_settings",
        lambda: SimpleNamespace(api_bearer_token="", api_key="", api_worker_scope="*"),
    )
    client = TestClient(app)

    response = client.get("/api/v1/workers/analyst-01/im-config")

    assert response.status_code == 200
    assert response.json()["credentials"]["slack"]["bot_token"] == "xoxb****"


def test_put_im_config_rejects_invalid_payload(tmp_path: Path, monkeypatch) -> None:
    app = _build_app(tmp_path)
    monkeypatch.setattr(
        "src.api.auth.get_settings",
        lambda: SimpleNamespace(api_bearer_token="", api_key="", api_worker_scope="*"),
    )
    client = TestClient(app)

    response = client.put(
        "/api/v1/workers/analyst-01/im-config",
        json={
            "persona": {
                "channels": [{
                    "type": "slack",
                    "connection_mode": "socket_mode",
                    "chat_ids": ["C123"],
                    "reply_mode": "streaming",
                    "features": {},
                }]
            },
            "credentials": {
                "slack": {
                    "bot_token": "bad-token",
                }
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["credentials", "slack", "bot_token"]


def test_reload_im_config_uses_existing_reload_function(tmp_path: Path, monkeypatch) -> None:
    app = _build_app(tmp_path)
    monkeypatch.setattr(
        "src.api.auth.get_settings",
        lambda: SimpleNamespace(api_bearer_token="", api_key="", api_worker_scope="*"),
    )
    reload_mock = AsyncMock(return_value={"channel_registry_refreshed": True})
    monkeypatch.setattr("src.api.routes.im_config_routes.reload_worker_runtime_state", reload_mock)
    client = TestClient(app)

    response = client.post("/api/v1/workers/analyst-01/im-config/reload")

    assert response.status_code == 200
    assert response.json()["status"] == "reloaded"
    assert response.json()["channels_refreshed"] == ["slack"]
    reload_mock.assert_awaited_once()


def test_put_im_config_allows_partial_credential_updates(tmp_path: Path, monkeypatch) -> None:
    app = _build_app(tmp_path)
    monkeypatch.setattr(
        "src.api.auth.get_settings",
        lambda: SimpleNamespace(api_bearer_token="", api_key="", api_worker_scope="*"),
    )
    client = TestClient(app)

    response = client.put(
        "/api/v1/workers/analyst-01/im-config",
        json={
            "persona": {
                "channels": [{
                    "type": "slack",
                    "connection_mode": "socket_mode",
                    "chat_ids": ["C123", "C999"],
                    "reply_mode": "streaming",
                    "features": {},
                }]
            },
            "credentials": {
                "slack": {
                    "team_id": "T999",
                }
            },
        },
    )

    assert response.status_code == 200
    saved = client.get("/api/v1/workers/analyst-01/im-config")
    assert saved.status_code == 200
    assert saved.json()["persona"]["channels"][0]["chat_ids"] == ["C123", "C999"]
    assert saved.json()["credentials"]["slack"]["team_id"] == "T999"
    assert saved.json()["credentials"]["slack"]["bot_token"] == "xoxb****"


def test_get_im_config_status_returns_runtime_snapshot(tmp_path: Path, monkeypatch) -> None:
    app = _build_app(tmp_path)
    monkeypatch.setattr(
        "src.api.auth.get_settings",
        lambda: SimpleNamespace(api_bearer_token="", api_key="", api_worker_scope="*"),
    )
    client = TestClient(app)

    response = client.get("/api/v1/workers/analyst-01/im-config/status")

    assert response.status_code == 200
    assert response.json()["adapters"][0]["registered"] is True
    assert response.json()["adapters"][0]["healthy"] is True


def test_im_config_routes_require_auth_when_configured(tmp_path: Path, monkeypatch) -> None:
    app = _build_app(tmp_path)
    monkeypatch.setattr(
        "src.api.auth.get_settings",
        lambda: SimpleNamespace(api_bearer_token="secret-token", api_key="", api_worker_scope="*"),
    )
    client = TestClient(app)

    response = client.get("/api/v1/workers/analyst-01/im-config")

    assert response.status_code == 401
