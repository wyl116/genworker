"""Feishu platform client."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.services._http_base import BaseAPIClient, BaseAPIConfig

from .config import FeishuConfig
from .exceptions import FeishuAPIError


@dataclass(frozen=True)
class FileMetadata:
    name: str
    path: str
    size: int
    file_type: str
    modified_at: str
    owner: str
    permissions: str


class FeishuClient:
    """Minimal Feishu OpenAPI wrapper."""

    def __init__(
        self,
        config: FeishuConfig,
        auth_provider: Any | None = None,
        http_client: BaseAPIClient | None = None,
    ) -> None:
        self._config = config
        self._auth = auth_provider
        self._http = http_client or BaseAPIClient(
            BaseAPIConfig(base_url=config.base_url)
        )

    async def download(self, source: dict, path: str, token: str) -> bytes:
        file_token = _extract_token(source, path)
        response = await self._http.get(
            f"/drive/v1/files/{file_token}/download",
            token=token,
        )
        return response if isinstance(response, bytes) else bytes(str(response), "utf-8")

    async def upload(
        self,
        source: dict,
        path: str,
        content: bytes,
        token: str,
    ) -> None:
        folder_token = source.get("folder_token", "")
        await self._http.post(
            "/drive/v1/files/upload_all",
            token=token,
            data=content,
            params={"file_name": path.split("/")[-1], "parent_type": "explorer", "parent_node": folder_token},
        )

    async def list_files(self, source: dict, path: str, token: str) -> list[str]:
        return [item.name for item in await self.list_with_metadata(source, path, token)]

    async def list_with_metadata(
        self,
        source: dict,
        path: str,
        token: str,
    ) -> list[FileMetadata]:
        folder_token = source.get("folder_token") or path.strip("/")
        response = await self._http.get(
            "/drive/v1/files",
            token=token,
            params={"folder_token": folder_token},
        )
        items = response.get("data", {}).get("files", [])
        results: list[FileMetadata] = []
        for item in items:
            results.append(FileMetadata(
                name=str(item.get("name", "")),
                path=f"{path.rstrip('/')}/{item.get('name', '')}",
                size=int(item.get("size", 0) or 0),
                file_type=str(item.get("type", "file")),
                modified_at=str(item.get("modified_time", item.get("modified_at", ""))),
                owner=str(item.get("owner_id", "")),
                permissions=str(item.get("permissions", "read")),
            ))
        return results

    async def send_message(
        self,
        recipients: tuple[str, ...] | list[str],
        content: str,
        token: str = "",
    ) -> dict[str, Any]:
        access_token = token or await self._get_token()
        response = await self._http.post(
            "/im/v1/messages",
            token=access_token,
            params={"receive_id_type": "user_id"},
            json={
                "receive_id": recipients[0] if recipients else "",
                "msg_type": "text",
                "content": '{"text": "%s"}' % content.replace('"', '\\"'),
            },
        )
        if response.get("code", 0) not in (0, None):
            raise FeishuAPIError("Feishu send message failed", payload=response)
        return response

    async def send_chat_message(
        self,
        chat_id: str,
        content: str,
        *,
        msg_type: str = "text",
        token: str = "",
    ) -> dict[str, Any]:
        access_token = token or await self._get_token()
        payload_content = (
            json.dumps({"text": content}, ensure_ascii=False)
            if msg_type == "text"
            else content
        )
        response = await self._http.post(
            "/im/v1/messages",
            token=access_token,
            params={"receive_id_type": "chat_id"},
            json={
                "receive_id": chat_id,
                "msg_type": msg_type,
                "content": payload_content,
            },
        )
        if response.get("code", 0) not in (0, None):
            raise FeishuAPIError("Feishu send chat message failed", payload=response)
        return response

    async def reply_message(
        self,
        message_id: str,
        content: str,
        *,
        msg_type: str = "text",
        token: str = "",
    ) -> dict[str, Any]:
        access_token = token or await self._get_token()
        payload_content = (
            json.dumps({"text": content}, ensure_ascii=False)
            if msg_type == "text"
            else content
        )
        response = await self._http.post(
            f"/im/v1/messages/{message_id}/reply",
            token=access_token,
            json={
                "msg_type": msg_type,
                "content": payload_content,
            },
        )
        if response.get("code", 0) not in (0, None):
            raise FeishuAPIError("Feishu reply message failed", payload=response)
        return response

    async def send_card(
        self,
        chat_id: str,
        card: dict[str, Any],
        *,
        token: str = "",
    ) -> dict[str, Any]:
        return await self.send_chat_message(
            chat_id,
            json.dumps(card, ensure_ascii=False),
            msg_type="interactive",
            token=token,
        )

    async def update_card(
        self,
        message_id: str,
        card: dict[str, Any],
        *,
        token: str = "",
    ) -> dict[str, Any]:
        access_token = token or await self._get_token()
        response = await self._http.request(
            "PATCH",
            f"/im/v1/messages/{message_id}",
            token=access_token,
            json={
                "content": json.dumps(card, ensure_ascii=False),
            },
        )
        if response.get("code", 0) not in (0, None):
            raise FeishuAPIError("Feishu update card failed", payload=response)
        return response

    async def read_doc_blocks(self, doc_token: str, token: str) -> list[dict]:
        response = await self._http.get(
            f"/docx/v1/documents/{doc_token}/blocks",
            token=token,
        )
        return list(response.get("data", {}).get("items", []))

    async def update_doc_blocks(
        self,
        doc_token: str,
        blocks: list[dict],
        token: str,
    ) -> bool:
        for block in blocks:
            block_id = block.get("block_id")
            await self._http.request(
                "PATCH",
                f"/docx/v1/documents/{doc_token}/blocks/{block_id}",
                token=token,
                json=block,
            )
        return True

    async def _get_token(self) -> str:
        if self._auth is None:
            return ""
        token, _ = await self._auth.get_token("default")
        return token


def _extract_token(source: dict[str, Any], path: str) -> str:
    return str(source.get("file_token") or path.strip("/").split("/")[-1])
