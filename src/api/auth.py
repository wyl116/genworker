"""Minimal API auth helpers."""
from __future__ import annotations

from fastapi import HTTPException, Request, status

from src.common.settings import get_settings


def require_api_auth(request: Request) -> None:
    """Accept optional Bearer token or X-API-Key when configured."""
    settings = get_settings()
    expected_bearer = str(getattr(settings, "api_bearer_token", "") or "").strip()
    expected_api_key = str(getattr(settings, "api_key", "") or "").strip()
    if not expected_bearer and not expected_api_key:
        return

    auth_header = str(request.headers.get("authorization", "") or "").strip()
    api_key = str(request.headers.get("x-api-key", "") or "").strip()
    bearer = ""
    if auth_header.lower().startswith("bearer "):
        bearer = auth_header[7:].strip()
    if (expected_bearer and bearer == expected_bearer) or (
        expected_api_key and api_key == expected_api_key
    ):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
    )


def enforce_worker_scope(request: Request, worker_id: str) -> None:
    """Restrict write actions to the configured or request-scoped worker list."""
    settings = get_settings()
    configured_scope = str(getattr(settings, "api_worker_scope", "*") or "*").strip()
    request_scope = str(request.headers.get("x-worker-scope", "") or "").strip()
    raw_scope = request_scope or configured_scope
    if not raw_scope or raw_scope == "*":
        return
    allowed = {item.strip() for item in raw_scope.split(",") if item.strip()}
    if worker_id in allowed:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"worker '{worker_id}' is outside caller scope",
    )
