"""
Shared async HTTP client helpers for platform integrations.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BaseAPIConfig:
    """Basic HTTP client configuration."""

    base_url: str
    timeout: float = 30.0
    max_retries: int = 3
    retry_delay: float = 1.0


class BaseAPIError(Exception):
    """Raised when a platform HTTP request fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class BaseAPIClient:
    """Thin wrapper over `httpx.AsyncClient` with token injection and retries."""

    def __init__(self, config: BaseAPIConfig) -> None:
        self._config = config
        self._client = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: str = "",
        auth_mode: str = "bearer",
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Send an HTTP request and decode JSON when possible."""
        import httpx

        request_params = dict(params or {})
        request_headers = dict(headers or {})
        if token:
            if auth_mode == "bearer":
                request_headers["Authorization"] = f"Bearer {token}"
            elif auth_mode == "query_param":
                request_params["access_token"] = token
            else:
                raise ValueError(f"Unsupported auth_mode: {auth_mode}")

        last_error: Exception | None = None
        for attempt in range(self._config.max_retries):
            try:
                client = await self._get_client()
                response = await client.request(
                    method=method.upper(),
                    url=path,
                    json=json,
                    params=request_params or None,
                    content=data,
                    headers=request_headers or None,
                )
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "application/json" in content_type:
                    return response.json()
                try:
                    return response.json()
                except Exception:
                    return response.content
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                payload = None
                try:
                    payload = exc.response.json()
                except Exception:
                    payload = exc.response.text
                if 500 <= status_code < 600 and attempt + 1 < self._config.max_retries:
                    last_error = exc
                    await asyncio.sleep(self._config.retry_delay * (attempt + 1))
                    continue
                raise BaseAPIError(
                    f"HTTP request failed: {status_code}",
                    status_code=status_code,
                    payload=payload,
                ) from exc
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt + 1 >= self._config.max_retries:
                    raise BaseAPIError("HTTP request failed") from exc
                await asyncio.sleep(self._config.retry_delay * (attempt + 1))

        raise BaseAPIError("HTTP request failed") from last_error

    async def get(self, path: str, *, token: str = "", **kwargs: Any) -> Any:
        return await self.request("GET", path, token=token, **kwargs)

    async def post(self, path: str, *, token: str = "", **kwargs: Any) -> Any:
        return await self.request("POST", path, token=token, **kwargs)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_client(self):
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url,
                timeout=self._config.timeout,
            )
        return self._client
