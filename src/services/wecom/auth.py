"""WeCom auth provider."""
from __future__ import annotations

from src.services._http_base import BaseAPIClient, BaseAPIConfig

from .config import WeComConfig


class WeComAuth:
    """Fetch WeCom access tokens."""

    def __init__(
        self,
        config: WeComConfig,
        http_client: BaseAPIClient | None = None,
    ) -> None:
        self._config = config
        self._http = http_client or BaseAPIClient(
            BaseAPIConfig(base_url=config.base_url)
        )

    async def get_token(self, _scope_key: str) -> tuple[str, int]:
        response = await self._http.get(
            "/gettoken",
            params={
                "corpid": self._config.corpid,
                "corpsecret": self._config.corpsecret,
            },
        )
        return str(response.get("access_token", "")), int(response.get("expires_in", 7200) or 7200)
