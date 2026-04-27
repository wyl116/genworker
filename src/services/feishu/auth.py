"""Feishu auth provider."""
from __future__ import annotations

from src.services._http_base import BaseAPIClient, BaseAPIConfig

from .config import FeishuConfig


class FeishuAuth:
    """Fetch Feishu tenant access tokens."""

    def __init__(
        self,
        config: FeishuConfig,
        http_client: BaseAPIClient | None = None,
    ) -> None:
        self._config = config
        self._http = http_client or BaseAPIClient(
            BaseAPIConfig(base_url=config.base_url)
        )

    async def get_token(self, _scope_key: str) -> tuple[str, int]:
        payload = {
            "app_id": self._config.app_id,
            "app_secret": self._config.app_secret,
        }
        response = await self._http.post(
            "/auth/v3/tenant_access_token/internal",
            json=payload,
        )
        token = str(response.get("tenant_access_token", ""))
        ttl = int(response.get("expire", 7200) or 7200)
        return token, ttl
