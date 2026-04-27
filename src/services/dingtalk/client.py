"""DingTalk platform client."""
from __future__ import annotations

from typing import Any

from src.services._http_base import BaseAPIClient, BaseAPIConfig

from .config import DingTalkConfig
from .exceptions import DingTalkAPIError


class DingTalkClient:
    """DingTalk wrapper with v1 legacy and v2 bearer-auth clients."""

    def __init__(
        self,
        config: DingTalkConfig,
        auth_provider: Any | None = None,
        v2_client: BaseAPIClient | None = None,
        v1_client: BaseAPIClient | None = None,
    ) -> None:
        self._config = config
        self._auth = auth_provider
        self._v2_client = v2_client or BaseAPIClient(
            BaseAPIConfig(base_url=config.base_url)
        )
        self._v1_client = v1_client or BaseAPIClient(
            BaseAPIConfig(base_url=config.legacy_base_url)
        )

    async def download(self, source: dict, path: str, token: str) -> bytes:
        media_id = source.get("media_id") or path.strip("/").split("/")[-1]
        response = await self._v1_client.get(
            "/media/downloadFile",
            token=token,
            auth_mode="query_param",
            params={"media_id": media_id},
        )
        return response if isinstance(response, bytes) else bytes(str(response), "utf-8")

    async def upload(self, source: dict, path: str, content: bytes, token: str) -> None:
        await self._v1_client.post(
            "/media/upload",
            token=token,
            auth_mode="query_param",
            params={"type": source.get("type", "file")},
            data=content,
        )

    async def list_files(self, source: dict, path: str, token: str) -> list[str]:
        response = await self._v1_client.post(
            "/topapi/storage/file/list",
            token=token,
            auth_mode="query_param",
            json={"union_id": source.get("union_id", ""), "path": path},
        )
        return [str(item.get("name", "")) for item in response.get("result", {}).get("items", [])]

    async def send_message(
        self,
        recipients: tuple[str, ...] | list[str],
        content: str,
        token: str = "",
    ) -> dict[str, Any]:
        access_token = token or await self._get_token()
        response = await self._v2_client.post(
            "/v1.0/robot/oToMessages/batchSend",
            token=access_token,
            auth_mode="bearer",
            json={
                "robotCode": self._config.robot_code,
                "userIds": list(recipients),
                "msgKey": "sampleText",
                "msgParam": {"content": content},
            },
        )
        if str(response.get("errcode", "0")) not in ("0", ""):
            raise DingTalkAPIError("DingTalk send message failed", payload=response)
        return response

    async def reply_message(
        self,
        conversation_id: str,
        content: str,
        *,
        msg_type: str = "text",
        token: str = "",
    ) -> dict[str, Any]:
        access_token = token or await self._get_token()
        msg_key = "sampleText"
        msg_param: dict[str, Any] = {"content": content}
        if msg_type == "markdown":
            msg_key = "sampleMarkdown"
            msg_param = {"title": "Reply", "text": content}
        response = await self._v2_client.post(
            "/v1.0/robot/groupMessages/send",
            token=access_token,
            auth_mode="bearer",
            json={
                "robotCode": self._config.robot_code,
                "conversationId": conversation_id,
                "msgKey": msg_key,
                "msgParam": msg_param,
            },
        )
        if str(response.get("errcode", "0")) not in ("0", ""):
            raise DingTalkAPIError("DingTalk reply message failed", payload=response)
        return response

    async def send_action_card(
        self,
        conversation_id: str,
        card: dict[str, Any],
        *,
        token: str = "",
    ) -> dict[str, Any]:
        access_token = token or await self._get_token()
        response = await self._v2_client.post(
            "/v1.0/robot/groupMessages/send",
            token=access_token,
            auth_mode="bearer",
            json={
                "robotCode": self._config.robot_code,
                "conversationId": conversation_id,
                "msgKey": "sampleActionCard",
                "msgParam": card,
            },
        )
        if str(response.get("errcode", "0")) not in ("0", ""):
            raise DingTalkAPIError("DingTalk action card failed", payload=response)
        return response

    async def send_interactive_card(
        self,
        conversation_id: str,
        card: dict[str, Any],
        *,
        token: str = "",
    ) -> dict[str, Any]:
        """Compatibility wrapper for stream-style interactive card send."""
        return await self.send_action_card(conversation_id, card, token=token)

    async def update_card(
        self,
        card_instance_id: str,
        card: dict[str, Any],
        *,
        token: str = "",
    ) -> dict[str, Any]:
        access_token = token or await self._get_token()
        response = await self._v2_client.request(
            "PUT",
            f"/v1.0/robot/interactiveCards/{card_instance_id}",
            token=access_token,
            auth_mode="bearer",
            json=card,
        )
        if str(response.get("errcode", "0")) not in ("0", ""):
            raise DingTalkAPIError("DingTalk update card failed", payload=response)
        return response

    async def get_user(self, user_id: str, token: str = "") -> dict[str, Any]:
        access_token = token or await self._get_token()
        response = await self._v1_client.post(
            "/topapi/v2/user/get",
            token=access_token,
            auth_mode="query_param",
            json={"userid": user_id},
        )
        return dict(response.get("result", {}))

    async def list_department_users(
        self,
        dept_id: int,
        cursor: int = 0,
        size: int = 100,
        token: str = "",
    ) -> list[dict[str, Any]]:
        access_token = token or await self._get_token()
        response = await self._v1_client.post(
            "/topapi/v2/user/list",
            token=access_token,
            auth_mode="query_param",
            json={"dept_id": dept_id, "cursor": cursor, "size": size},
        )
        return list(response.get("result", {}).get("list", []))

    async def _get_token(self) -> str:
        if self._auth is None:
            return ""
        token, _ = await self._auth.get_token("default")
        return token
