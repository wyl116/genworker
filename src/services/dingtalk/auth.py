"""DingTalk auth provider."""
from __future__ import annotations

from src.services._http_base import BaseAPIClient, BaseAPIConfig

from .config import DingTalkConfig


class DingTalkAuth:
    """Fetch DingTalk access tokens."""

    def __init__(
        self,
        config: DingTalkConfig,
        http_client: BaseAPIClient | None = None,
    ) -> None:
        self._config = config
        self._http = http_client or BaseAPIClient(
            BaseAPIConfig(base_url=config.base_url)
        )

    async def get_token(self, _scope_key: str) -> tuple[str, int]:
        response = await self._http.post(
            "/v1.0/oauth2/accessToken",
            json={
                "appKey": self._config.app_key,
                "appSecret": self._config.app_secret,
            },
        )
        return str(response.get("accessToken", "")), int(response.get("expireIn", 7200) or 7200)
