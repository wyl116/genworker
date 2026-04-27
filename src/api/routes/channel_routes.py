"""Routes for IM channel webhooks."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/v1/channel", tags=["channel"])


async def _handle(adapter_id: str, request: Request):
    registry = getattr(request.app.state, "im_channel_registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="IM channel registry not initialized")
    adapter = registry.find_by_adapter_id(adapter_id)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"Adapter '{adapter_id}' not found")
    result = await adapter.handle_webhook(request)
    return result if result is not None else {"status": "ok"}


async def _handle_named(adapter_id: str, request: Request, handler_name: str):
    registry = getattr(request.app.state, "im_channel_registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="IM channel registry not initialized")
    adapter = registry.find_by_adapter_id(adapter_id)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"Adapter '{adapter_id}' not found")
    handler = getattr(adapter, handler_name, None)
    if not callable(handler):
        raise HTTPException(status_code=404, detail=f"Adapter '{adapter_id}' does not support {handler_name}")
    result = await handler(request)
    return result if result is not None else {"status": "ok"}


@router.get("")
async def list_channels(request: Request):
    registry = getattr(request.app.state, "im_channel_registry", None)
    if registry is None:
        return {"channels": []}
    return {"channels": registry.list_adapters()}


@router.get("/{adapter_id}/status")
async def channel_status(adapter_id: str, request: Request):
    registry = getattr(request.app.state, "im_channel_registry", None)
    manager = getattr(request.app.state, "channel_manager", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="IM channel registry not initialized")
    adapter = registry.find_by_adapter_id(adapter_id)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"Adapter '{adapter_id}' not found")
    healthy = await adapter.health_check() if manager is None else (await manager.health_check()).get(adapter_id, False)
    details = getattr(adapter, "status_snapshot", None)
    payload = {"adapter_id": adapter_id, "healthy": healthy}
    if callable(details):
        payload["details"] = details()
    return payload


@router.post("/webhooks/email")
async def email_webhook(request: Request):
    registry = getattr(request.app.state, "im_channel_registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="IM channel registry not initialized")
    adapter = None
    for adapter_id in registry.list_adapters():
        if adapter_id.startswith("email:"):
            adapter = registry.get_adapter(adapter_id)
            break
    if adapter is None:
        return {"status": "no_adapter"}
    result = await adapter.handle_webhook(request)
    return result if result is not None else {"status": "ok"}


@router.get("/{adapter_id}/webhook")
async def channel_webhook_get(adapter_id: str, request: Request):
    return await _handle(adapter_id, request)


@router.post("/{adapter_id}/webhook")
async def channel_webhook_post(adapter_id: str, request: Request):
    return await _handle(adapter_id, request)


@router.post("/{adapter_id}/interactivity")
async def channel_interactivity_post(adapter_id: str, request: Request):
    return await _handle_named(adapter_id, request, "handle_interactivity")


@router.post("/{adapter_id}/slash")
async def channel_slash_post(adapter_id: str, request: Request):
    return await _handle_named(adapter_id, request, "handle_slash_command")
