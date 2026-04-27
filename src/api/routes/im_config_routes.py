"""Worker IM config routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.api.auth import enforce_worker_scope, require_api_auth
from src.api.im_config_loader import load_worker_im_config, write_worker_im_config
from src.api.im_config_validators import (
    IMConfigValidationError,
    validate_im_config_payload,
)
from src.runtime.worker_reload import reload_worker_runtime_state

router = APIRouter(prefix="/api/v1/workers", tags=["im-config"])


class IMChannelConfig(BaseModel):
    type: str
    connection_mode: str
    chat_ids: list[str] = Field(default_factory=list)
    reply_mode: str = "complete"
    features: dict[str, Any] = Field(default_factory=dict)


class PersonaConfig(BaseModel):
    channels: list[IMChannelConfig] = Field(default_factory=list)


class FeishuCredentialInput(BaseModel):
    app_id: str = ""
    app_secret: str = ""


class SlackCredentialInput(BaseModel):
    bot_token: str = ""
    app_token: str = ""
    signing_secret: str = ""
    team_id: str = ""


class CredentialConfig(BaseModel):
    feishu: FeishuCredentialInput | None = None
    slack: SlackCredentialInput | None = None


class IMConfigUpdateRequest(BaseModel):
    persona: PersonaConfig
    credentials: CredentialConfig = Field(default_factory=CredentialConfig)


@router.get("/{worker_id}/im-config")
async def get_im_config(
    worker_id: str,
    request: Request,
    tenant_id: str = Query(default="demo"),
) -> dict[str, Any]:
    require_api_auth(request)
    try:
        payload = load_worker_im_config(
            workspace_root=getattr(request.app.state, "workspace_root", "workspace"),
            tenant_id=tenant_id,
            worker_id=worker_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "worker_id": worker_id,
        "persona": payload["persona"],
        "credentials": payload["masked_credentials"],
    }


@router.put("/{worker_id}/im-config")
async def put_im_config(
    worker_id: str,
    body: IMConfigUpdateRequest,
    request: Request,
    tenant_id: str = Query(default="demo"),
) -> dict[str, Any]:
    require_api_auth(request)
    enforce_worker_scope(request, worker_id)

    channels = [item.model_dump() for item in body.persona.channels]
    credentials = body.credentials.model_dump(exclude_none=True)
    try:
        existing_payload = load_worker_im_config(
            workspace_root=getattr(request.app.state, "workspace_root", "workspace"),
            tenant_id=tenant_id,
            worker_id=worker_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    merged_credentials = _merge_existing_credentials(
        existing=existing_payload.get("credentials", {}),
        incoming=credentials,
    )
    try:
        normalized_channels, normalized_credentials = validate_im_config_payload(
            channels=channels,
            credentials=merged_credentials,
        )
    except IMConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.details) from exc

    try:
        result = write_worker_im_config(
            workspace_root=getattr(request.app.state, "workspace_root", "workspace"),
            tenant_id=tenant_id,
            worker_id=worker_id,
            channels=normalized_channels,
            credentials=normalized_credentials,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "status": "saved",
        **result,
    }


@router.post("/{worker_id}/im-config/reload")
async def reload_im_config(
    worker_id: str,
    request: Request,
    tenant_id: str = Query(default="demo"),
) -> dict[str, Any]:
    require_api_auth(request)
    enforce_worker_scope(request, worker_id)

    context = getattr(request.app.state, "bootstrap_context", None)
    worker_router = getattr(request.app.state, "worker_router", None)
    if context is None or worker_router is None:
        raise HTTPException(status_code=503, detail="runtime reload unavailable")

    try:
        result = await reload_worker_runtime_state(
            app=request.app,
            context=context,
            worker_router=worker_router,
            worker_id=worker_id,
            tenant_id=tenant_id,
            trigger_source="im_config_reload",
            changed_files=("PERSONA.md", "CHANNEL_CREDENTIALS.json"),
        )
        config = load_worker_im_config(
            workspace_root=getattr(request.app.state, "workspace_root", "workspace"),
            tenant_id=tenant_id,
            worker_id=worker_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    channels_refreshed = sorted({
        str(item.get("type", "")).strip().lower()
        for item in config["persona"]["channels"]
        if str(item.get("type", "")).strip()
    })
    return {
        "status": "reloaded",
        "worker_id": worker_id,
        "channels_refreshed": channels_refreshed,
        "runtime": result,
    }


@router.get("/{worker_id}/im-config/status")
async def get_im_config_status(
    worker_id: str,
    request: Request,
    tenant_id: str = Query(default="demo"),
) -> dict[str, Any]:
    require_api_auth(request)
    try:
        config = load_worker_im_config(
            workspace_root=getattr(request.app.state, "workspace_root", "workspace"),
            tenant_id=tenant_id,
            worker_id=worker_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    registry = getattr(request.app.state, "im_channel_registry", None)
    adapters: list[dict[str, Any]] = []
    for raw in config["persona"]["channels"]:
        channel_type = str(raw.get("type", "")).strip().lower()
        adapter_id = f"{channel_type}:{tenant_id}:{worker_id}"
        adapter = registry.get_adapter(adapter_id) if registry is not None else None
        snapshot = adapter.status_snapshot() if adapter is not None and hasattr(adapter, "status_snapshot") else {}
        healthy = False
        if adapter is not None and hasattr(adapter, "health_check"):
            healthy = await adapter.health_check()
        adapters.append({
            "type": channel_type,
            "registered": adapter is not None,
            "connection_mode": str(raw.get("connection_mode", "")).strip().lower(),
            "healthy": healthy,
            "chat_ids": list(raw.get("chat_ids", [])),
            "last_error": str(snapshot.get("last_error", "") or getattr(adapter, "_last_error", "") if adapter is not None else ""),
        })
    return {"adapters": adapters}


def _merge_existing_credentials(
    *,
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Merge partial credential updates onto existing persisted credentials."""
    merged: dict[str, Any] = {}
    for platform in ("feishu", "slack"):
        current = existing.get(platform)
        next_value = incoming.get(platform)
        if isinstance(current, dict):
            merged[platform] = dict(current)
        if isinstance(next_value, dict):
            merged.setdefault(platform, {})
            merged[platform].update({
                str(key): value
                for key, value in next_value.items()
                if str(value or "").strip()
            })
    return merged
