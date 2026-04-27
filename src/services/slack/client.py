"""Slack platform client."""
from __future__ import annotations

import time
from typing import Any

import httpx

try:
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_client import AsyncWebClient
    _SLACK_SDK_AVAILABLE = True
except ImportError as exc:  # pragma: no cover - exercised in sdk-missing environments
    _SLACK_SDK_AVAILABLE = False
    _SLACK_SDK_IMPORT_ERROR = exc

    class SlackApiError(Exception):
        """Fallback Slack API error when slack_sdk is unavailable."""

    class AsyncWebClient:  # type: ignore[no-redef]
        """Placeholder async web client for slack_sdk-free environments."""

        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = dict(kwargs)

from .config import SlackConfig
from .exceptions import SlackAPIError


class SlackClient:
    """Minimal Slack Web API wrapper."""

    def __init__(
        self,
        config: SlackConfig,
        auth_provider: Any | None = None,
        web_client: AsyncWebClient | None = None,
    ) -> None:
        self._config = config
        self._auth = auth_provider
        self._web = web_client or self._build_web_client(config)
        self._user_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._user_cache_ttl_seconds = 3600.0

    async def post_message(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._web.chat_postMessage(
                channel=channel,
                text=text,
                thread_ts=thread_ts,
            )
        except SlackApiError as exc:
            raise SlackAPIError("Slack post_message failed", payload=getattr(exc, "response", None)) from exc
        return _response_to_dict(response)

    async def update_message(
        self,
        channel: str,
        ts: str,
        *,
        text: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"channel": channel, "ts": ts}
        if text is not None:
            kwargs["text"] = text
        if blocks is not None:
            kwargs["blocks"] = blocks
        try:
            response = await self._web.chat_update(**kwargs)
        except SlackApiError as exc:
            raise SlackAPIError("Slack update_message failed", payload=getattr(exc, "response", None)) from exc
        return _response_to_dict(response)

    async def post_blocks(
        self,
        channel: str,
        blocks: dict[str, Any] | list[dict[str, Any]],
        *,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        normalized_blocks = blocks.get("blocks", []) if isinstance(blocks, dict) else list(blocks)
        try:
            response = await self._web.chat_postMessage(
                channel=channel,
                text=_fallback_text_from_blocks(normalized_blocks),
                blocks=normalized_blocks,
                thread_ts=thread_ts,
            )
        except SlackApiError as exc:
            raise SlackAPIError("Slack post_blocks failed", payload=getattr(exc, "response", None)) from exc
        return _response_to_dict(response)

    async def upload_file(
        self,
        channel: str,
        content: str,
        filename: str,
    ) -> dict[str, Any]:
        try:
            response = await self._web.files_upload_v2(
                channel=channel,
                content=content,
                filename=filename,
            )
        except SlackApiError as exc:
            raise SlackAPIError("Slack upload_file failed", payload=getattr(exc, "response", None)) from exc
        return _response_to_dict(response)

    async def users_info(self, user_id: str) -> dict[str, Any]:
        cached = self._user_cache.get(user_id)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self._user_cache_ttl_seconds:
            return cached[1]
        try:
            response = await self._web.users_info(user=user_id)
        except SlackApiError as exc:
            raise SlackAPIError("Slack users_info failed", payload=getattr(exc, "response", None)) from exc
        payload = _response_to_dict(response)
        self._user_cache[user_id] = (now, payload)
        return payload

    async def post_response_url(
        self,
        url: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
        response.raise_for_status()
        if not response.content:
            return {"ok": True, "status_code": response.status_code}
        try:
            return dict(response.json())
        except ValueError:
            return {"ok": True, "status_code": response.status_code, "text": response.text}

    def _build_web_client(self, config: SlackConfig) -> AsyncWebClient | "_MissingSlackWebClient":
        if _SLACK_SDK_AVAILABLE:
            return AsyncWebClient(
                token=config.bot_token,
                base_url=config.base_url,
            )
        return _MissingSlackWebClient()


class _MissingSlackWebClient:
    """Raise a consistent error when Slack API operations are used without slack_sdk."""

    def __getattr__(self, name: str) -> Any:
        async def _missing(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("slack_sdk is required for Slack Web API operations") from _SLACK_SDK_IMPORT_ERROR

        return _missing


def _response_to_dict(response: Any) -> dict[str, Any]:
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        return data
    if isinstance(response, dict):
        return response
    return dict(data or {})


def _fallback_text_from_blocks(blocks: list[dict[str, Any]]) -> str:
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, dict):
            candidate = str(text.get("text", "")).strip()
            if candidate:
                return candidate
    return "Slack message"
