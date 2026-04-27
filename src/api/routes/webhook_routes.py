"""Webhook ingress routes for passive push sensors."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from src.api.models.request_models import WebhookIngestRequest

router = APIRouter(prefix="/api/v1/webhook", tags=["webhook"])


@router.post("/ingest/{worker_id}")
async def ingest_webhook(
    worker_id: str,
    body: WebhookIngestRequest,
    request: Request,
) -> dict[str, str]:
    sensor_registries = getattr(request.app.state, "sensor_registries", {}) or {}
    registry = sensor_registries.get(worker_id)
    if registry is None:
        raise HTTPException(status_code=404, detail=f"Worker '{worker_id}' not found")

    webhook_sensor = registry.get_sensor("webhook")
    if webhook_sensor is None or not hasattr(webhook_sensor, "ingest"):
        raise HTTPException(
            status_code=400,
            detail=f"Worker '{worker_id}' has no webhook sensor configured",
        )

    await webhook_sensor.ingest(body.model_dump(exclude_none=True))
    return {"status": "accepted", "event_type": body.event_type}
