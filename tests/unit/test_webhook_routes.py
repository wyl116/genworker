# edition: baseline
from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.webhook_routes import router


def test_webhook_ingest_delegates_to_worker_sensor() -> None:
    webhook_sensor = AsyncMock()
    webhook_sensor.ingest = AsyncMock()
    registry = type("Registry", (), {"get_sensor": lambda self, sensor_type: webhook_sensor})()

    app = FastAPI()
    app.include_router(router)
    app.state.sensor_registries = {"worker-1": registry}

    client = TestClient(app)
    response = client.post(
        "/api/v1/webhook/ingest/worker-1",
        json={
            "event_type": "external.ci.build_failed",
            "data": {"build_id": "b-1"},
            "priority": 30,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "event_type": "external.ci.build_failed",
    }
    webhook_sensor.ingest.assert_awaited_once()
