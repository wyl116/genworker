"""WeCom platform client."""
from __future__ import annotations

from typing import Any

from src.services._http_base import BaseAPIClient, BaseAPIConfig

from .config import WeComConfig
from .exceptions import WeComAPIError


class WeComClient:
    """Minimal WeCom API wrapper."""

    def __init__(
        self,
        config: WeComConfig,
        auth_provider: Any | None = None,
        http_client: BaseAPIClient | None = None,
    ) -> None:
        self._config = config
        self._auth = auth_provider
        self._http = http_client or BaseAPIClient(
            BaseAPIConfig(base_url=config.base_url)
        )

    async def download(self, source: dict, path: str, token: str) -> bytes:
        media_id = source.get("media_id") or path.strip("/").split("/")[-1]
        response = await self._http.get(
            "/media/get",
            token=token,
            auth_mode="query_param",
            params={"media_id": media_id},
        )
        return response if isinstance(response, bytes) else bytes(str(response), "utf-8")

    async def upload(self, source: dict, path: str, content: bytes, token: str) -> None:
        await self._http.post(
            "/media/upload",
            token=token,
            auth_mode="query_param",
            params={"type": source.get("type", "file"), "filename": path.split("/")[-1]},
            data=content,
        )

    async def list_files(self, source: dict, path: str, token: str) -> list[str]:
        response = await self._http.post(
            "/wedrive/file_list",
            token=token,
            auth_mode="query_param",
            json={"spaceid": source.get("space_id", ""), "fatherid": source.get("folder_id", "")},
        )
        return [str(item.get("name", "")) for item in response.get("file_list", [])]

    async def send_message(
        self,
        recipients: tuple[str, ...] | list[str],
        content: str,
        token: str = "",
    ) -> dict[str, Any]:
        access_token = token or await self._get_token()
        response = await self._http.post(
            "/message/send",
            token=access_token,
            auth_mode="query_param",
            json={
                "touser": "|".join(recipients),
                "msgtype": "text",
                "agentid": self._config.agent_id,
                "text": {"content": content},
            },
        )
        if response.get("errcode", 0) != 0:
            raise WeComAPIError("WeCom send message failed", payload=response)
        return response

    async def reply_message(
        self,
        chat_id: str,
        content: str,
        *,
        msg_type: str = "text",
        token: str = "",
    ) -> dict[str, Any]:
        access_token = token or await self._get_token()
        if msg_type == "markdown":
            payload = {
                "chatid": chat_id,
                "msgtype": "markdown",
                "agentid": self._config.agent_id,
                "markdown": {"content": content},
            }
        else:
            payload = {
                "chatid": chat_id,
                "msgtype": "text",
                "agentid": self._config.agent_id,
                "text": {"content": content},
            }
        response = await self._http.post(
            "/message/send",
            token=access_token,
            auth_mode="query_param",
            json=payload,
        )
        if response.get("errcode", 0) != 0:
            raise WeComAPIError("WeCom reply message failed", payload=response)
        return response

    async def send_markdown(
        self,
        chat_id: str,
        markdown_content: str,
        *,
        token: str = "",
    ) -> dict[str, Any]:
        return await self.reply_message(
            chat_id,
            markdown_content,
            msg_type="markdown",
            token=token,
        )

    async def _get_token(self) -> str:
        if self._auth is None:
            return ""
        token, _ = await self._auth.get_token("default")
        return token
