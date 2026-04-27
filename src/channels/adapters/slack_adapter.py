"""Slack IM adapter implementation."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
import re
import time
from typing import Any, AsyncGenerator
from urllib.parse import parse_qs

try:
    from slack_sdk.socket_mode.aiohttp import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.web.async_client import AsyncWebClient
    _SLACK_SOCKET_MODE_SDK_AVAILABLE = True
except ImportError as exc:  # pragma: no cover - exercised in sdk-missing environments
    _SLACK_SOCKET_MODE_SDK_AVAILABLE = False
    _SLACK_SOCKET_MODE_IMPORT_ERROR = exc

    class SocketModeClient:  # type: ignore[no-redef]
        """Placeholder socket mode client for slack_sdk-free environments."""

        def __init__(self, **kwargs: Any) -> None:
            self.socket_mode_request_listeners: list[Any] = []
            self.kwargs = dict(kwargs)

        async def connect(self) -> None:
            raise RuntimeError("slack_sdk is required for Slack socket mode") from _SLACK_SOCKET_MODE_IMPORT_ERROR

        def is_connected(self) -> bool:
            return False

        async def close(self) -> None:
            return None

    class SocketModeRequest:  # type: ignore[no-redef]
        def __init__(self, envelope_id: str = "", type: str = "", payload: Any = None) -> None:
            self.envelope_id = envelope_id
            self.type = type
            self.payload = payload

    class SocketModeResponse:  # type: ignore[no-redef]
        def __init__(self, envelope_id: str = "") -> None:
            self.envelope_id = envelope_id

    class AsyncWebClient:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = dict(kwargs)

from src.common.logger import get_logger
from src.services.slack.client import SlackClient

from ._sdk_runtime import (
    ReconnectController,
    build_reconnect_controller,
    cancel_task,
    is_task_running,
    utc_now_iso,
)
from ..models import (
    Attachment,
    ChannelBinding,
    ChannelInboundMessage,
    Mention,
    ReplyContent,
    StreamChunk,
    freeze_data,
)
from ..protocol import MessageCallback

logger = get_logger()

_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")
_CHANNEL_RE = re.compile(r"<#([A-Z0-9]+)(?:\|([^>]+))?>")
_SUBTEAM_RE = re.compile(r"<!subteam\^([A-Z0-9]+)(?:\|([^>]+))?>")


class SlackIMAdapter:
    """Slack adapter supporting webhook and socket mode ingress."""

    channel_type = "slack"

    def __init__(
        self,
        client: SlackClient,
        bindings: tuple[ChannelBinding, ...],
    ) -> None:
        self._client = client
        self._bindings = bindings
        self._message_callback: MessageCallback | None = None
        self._router: Any | None = None
        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._socket_task: asyncio.Task[Any] | None = None
        self._socket_client: SocketModeClient | None = None
        self._connection_state = "stopped"
        self._last_error = ""
        self._last_connected_at = ""
        self._last_event_at = ""
        self._last_disconnect_at = ""
        self._degraded_reason = ""
        self._degraded_modes: dict[str, str] = {}
        self._chat_ids = {
            chat_id
            for binding in bindings
            for chat_id in binding.chat_ids
            if chat_id and chat_id != "*"
        }
        self._socket_bindings = tuple(
            binding for binding in bindings
            if binding.connection_mode == "socket_mode"
        )
        self._declared_modes = {
            str(binding.connection_mode or "").strip().lower() or "webhook"
            for binding in bindings
        }
        self._reconnect = self._build_reconnect_controller()

    def supports_streaming(self) -> bool:
        return True

    async def start(self, message_callback: MessageCallback) -> None:
        self._message_callback = message_callback
        self._router = getattr(message_callback, "__self__", None)
        self._started = True
        self._loop = asyncio.get_running_loop()
        self._connection_state = "starting"
        self._last_error = ""
        self._degraded_modes = {}
        self._degraded_reason = ""
        self._reconnect.reset()
        self._apply_mode_degradation()
        if "socket_mode" in self._declared_modes and "socket_mode" not in self._degraded_modes:
            await self._start_socket_client()
        elif "webhook" in self._declared_modes and "webhook" not in self._degraded_modes:
            self._connection_state = "ready"
        else:
            self._connection_state = "degraded"
        self._sync_degraded_reason()

    async def stop(self) -> None:
        self._started = False
        self._connection_state = "stopped"
        await self._stop_socket_client()

    async def health_check(self) -> bool:
        if not self._started:
            return False
        available_modes = set(self._declared_modes) - set(self._degraded_modes)
        if not available_modes:
            return False
        if "socket_mode" in available_modes and "webhook" not in available_modes:
            return self._connection_state == "connected"
        return True

    def status_snapshot(self) -> dict[str, Any]:
        websocket_enabled = "socket_mode" in self._declared_modes
        available_modes = sorted(set(self._declared_modes) - set(self._degraded_modes))
        healthy = bool(available_modes) and (
            self._connection_state == "connected"
            or "socket_mode" not in available_modes
            or "webhook" in available_modes
        )
        return {
            "channel_type": self.channel_type,
            "started": self._started,
            "healthy": self._started and healthy,
            "connection_state": self._connection_state,
            "active_modes": available_modes,
            "declared_modes": sorted(self._declared_modes),
            "websocket_enabled": websocket_enabled,
            "socket_mode_enabled": websocket_enabled,
            "last_error": self._last_error,
            "last_connected_at": self._last_connected_at,
            "last_event_at": self._last_event_at,
            "last_disconnect_at": self._last_disconnect_at,
            "degraded_modes": dict(self._degraded_modes),
            "degraded_reason": self._degraded_reason,
            **self._reconnect.snapshot(),
        }

    async def parse_event(self, raw_event: Any) -> ChannelInboundMessage | None:
        if not isinstance(raw_event, dict):
            return None
        if str(raw_event.get("type", "")).strip() != "event_callback":
            return None
        event = raw_event.get("event", {}) or {}
        event_type = str(event.get("type", "")).strip()
        if event_type not in {"message", "app_mention"}:
            return None
        subtype = str(event.get("subtype", "")).strip()
        if event.get("bot_id") or subtype == "bot_message":
            return None
        if subtype and subtype not in {"file_share"}:
            return None
        channel_id = str(event.get("channel", "")).strip()
        if self._chat_ids and channel_id not in self._chat_ids:
            return None
        sender_id = str(event.get("user", "")).strip()
        thread_ts = str(event.get("thread_ts", "")).strip()
        message_ts = str(event.get("ts", "")).strip()
        mentions = list(
            Mention(user_id=user_id, name=user_id, is_bot=False)
            for user_id in _MENTION_RE.findall(str(event.get("text", "")))
        )
        if event_type == "app_mention":
            mentions.append(Mention(user_id="slack-app", name="slack-app", is_bot=True))
        attachments = tuple(
            Attachment(
                file_key=str(item.get("id", "")),
                file_name=str(item.get("name", "")),
                file_type=str(item.get("filetype", item.get("mimetype", "file"))),
                size=int(item.get("size", 0) or 0),
            )
            for item in event.get("files", ())
            if isinstance(item, dict)
        )
        return ChannelInboundMessage(
            message_id=message_ts,
            channel_type=self.channel_type,
            chat_id=channel_id,
            chat_type=_normalize_chat_type(str(event.get("channel_type", ""))),
            sender_id=sender_id,
            sender_name=await self._resolve_user_name(sender_id),
            content=self._normalize_text(str(event.get("text", ""))),
            msg_type=subtype or "text",
            reply_to_id=thread_ts if thread_ts and thread_ts != message_ts else None,
            mentions=tuple(mentions),
            attachments=attachments,
            raw_event=freeze_data(raw_event),
            metadata=freeze_data({
                "team_id": raw_event.get("team_id") or getattr(self._client._config, "team_id", ""),
                "channel_type": event.get("channel_type", ""),
                "thread_ts": thread_ts,
                "event_id": raw_event.get("event_id", ""),
            }),
        )

    async def reply(
        self,
        source_msg: ChannelInboundMessage,
        content: ReplyContent,
    ) -> str:
        response_url = str(source_msg.metadata_dict.get("response_url", "")).strip()
        if response_url:
            return await self._post_response_url(response_url, content)
        thread_ts = source_msg.reply_to_id or source_msg.message_id
        if content.content_type == "card":
            response = await self._client.post_blocks(
                source_msg.chat_id,
                content.card_dict,
                thread_ts=thread_ts,
            )
        else:
            response = await self._client.post_message(
                source_msg.chat_id,
                content.text,
                thread_ts=thread_ts,
            )
        return _extract_ts(response)

    async def reply_stream(
        self,
        source_msg: ChannelInboundMessage,
        chunks: AsyncGenerator[StreamChunk, None],
    ) -> str:
        response_url = str(source_msg.metadata_dict.get("response_url", "")).strip()
        if response_url:
            aggregated = ""
            async for chunk in chunks:
                if chunk.chunk_type == "text_delta":
                    aggregated += chunk.content
                elif chunk.chunk_type == "finished":
                    break
            return await self._post_response_url(response_url, ReplyContent(text=aggregated))

        thread_ts = source_msg.reply_to_id or source_msg.message_id
        initial = await self._client.post_message(
            source_msg.chat_id,
            "⏳",
            thread_ts=thread_ts,
        )
        ts = _extract_ts(initial)
        aggregated = ""
        last_update = 0.0
        min_interval = self._update_interval_seconds()
        async for chunk in chunks:
            if chunk.chunk_type == "text_delta":
                aggregated += chunk.content
                now = time.monotonic()
                if now - last_update >= min_interval:
                    await self._client.update_message(source_msg.chat_id, ts, text=aggregated)
                    last_update = now
            elif chunk.chunk_type == "finished":
                break
        await self._client.update_message(source_msg.chat_id, ts, text=aggregated)
        return ts

    async def send_message(
        self,
        chat_id: str,
        content: ReplyContent,
    ) -> str:
        if content.content_type == "card":
            response = await self._client.post_blocks(chat_id, content.card_dict)
        else:
            response = await self._client.post_message(chat_id, content.text)
        return _extract_ts(response)

    async def handle_webhook(self, request: Any) -> Any:
        if "webhook" in self._degraded_modes:
            logger.warning("[SlackIMAdapter] webhook degraded: %s", self._degraded_modes["webhook"])
            return {"status": "degraded"}
        body = await request.body()
        if not self._verify_signature(body, request.headers):
            return {"status": "invalid_signature"}
        payload = json.loads(body.decode("utf-8") or "{}")
        payload_type = str(payload.get("type", "")).strip()
        if payload_type == "url_verification":
            return {"challenge": payload.get("challenge", "")}
        if payload_type == "event_callback":
            message = await self.parse_event(payload)
            if message is not None:
                self._last_event_at = utc_now_iso()
                self._schedule_inbound_message(message)
            return {"status": "ok"}
        return {"status": "ignored"}

    async def handle_interactivity(self, request: Any) -> Any:
        if "webhook" in self._degraded_modes:
            logger.warning("[SlackIMAdapter] interactivity degraded: %s", self._degraded_modes["webhook"])
            return {"status": "degraded"}
        body = await request.body()
        if not self._verify_signature(body, request.headers):
            return {"status": "invalid_signature"}
        payload = self._parse_form_payload(body)
        message = self._build_interaction_message(payload)
        if message is not None:
            self._last_event_at = utc_now_iso()
            self._schedule_command_message(message)
        return {}

    async def handle_slash_command(self, request: Any) -> Any:
        if "webhook" in self._degraded_modes:
            logger.warning("[SlackIMAdapter] slash degraded: %s", self._degraded_modes["webhook"])
            return {"status": "degraded"}
        body = await request.body()
        if not self._verify_signature(body, request.headers):
            return {"status": "invalid_signature"}
        payload = self._parse_form_payload(body)
        message = self._build_slash_message(payload)
        if message is not None:
            self._last_event_at = utc_now_iso()
            self._schedule_command_message(message)
        return {"response_type": "in_channel"}

    async def _start_socket_client(self) -> None:
        if not self._socket_bindings or is_task_running(self._socket_task):
            return

        async def _supervisor() -> None:
            while self._started:
                wait_seconds = self._reconnect.before_attempt()
                if wait_seconds > 0:
                    self._connection_state = "reconnecting"
                    self._degraded_modes["socket_mode"] = "circuit_open"
                    self._sync_degraded_reason()
                    await asyncio.sleep(wait_seconds)
                    if not self._started:
                        break
                try:
                    self._connection_state = "connecting"
                    self._socket_client = SocketModeClient(
                        app_token=self._client._config.app_token,
                        web_client=AsyncWebClient(token=self._client._config.bot_token),
                    )
                    self._socket_client.socket_mode_request_listeners.append(self._handle_socket_request)
                    await self._socket_client.connect()
                    await asyncio.sleep(self._startup_probe_seconds())
                    if not self._started:
                        break
                    self._reconnect.register_success()
                    self._degraded_modes.pop("socket_mode", None)
                    self._sync_degraded_reason()
                    self._connection_state = "connected"
                    self._last_connected_at = utc_now_iso()
                    while self._started and self._socket_client is not None and self._socket_client.is_connected():
                        await asyncio.sleep(0.2)
                    if not self._started:
                        break
                    self._reconnect.register_failure(RuntimeError("socket closed"))
                    self._last_disconnect_at = utc_now_iso()
                    self._degraded_modes["socket_mode"] = (
                        "circuit_open"
                        if self._reconnect.breaker_state == "open"
                        else "connection_error"
                    )
                    self._sync_degraded_reason()
                    await self._sleep_after_failure()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    self._reconnect.register_failure(exc)
                    self._last_disconnect_at = utc_now_iso()
                    self._connection_state = "reconnecting"
                    self._degraded_modes["socket_mode"] = (
                        "circuit_open"
                        if self._reconnect.breaker_state == "open"
                        else "connection_error"
                    )
                    self._sync_degraded_reason()
                    logger.exception("[SlackIMAdapter] Socket mode loop exited unexpectedly")
                    await self._sleep_after_failure()
                finally:
                    await self._safe_disconnect(self._socket_client)
                    self._socket_client = None
            if not self._started:
                self._connection_state = "stopped"
            elif "webhook" in (set(self._declared_modes) - set(self._degraded_modes)):
                self._connection_state = "ready"
            else:
                self._connection_state = "degraded"

        self._socket_task = asyncio.create_task(_supervisor(), name="slack-socket-mode")

    async def _stop_socket_client(self) -> None:
        await cancel_task(self._socket_task)
        self._socket_task = None
        await self._safe_disconnect(self._socket_client)
        self._socket_client = None

    async def _handle_socket_request(self, client: SocketModeClient, request: SocketModeRequest) -> None:
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        self._last_event_at = utc_now_iso()
        if request.type == "events_api":
            payload = request.payload if isinstance(request.payload, dict) else {}
            message = await self.parse_event(payload)
            if message is not None:
                self._schedule_inbound_message(message)
        elif request.type == "slash_commands":
            payload = request.payload if isinstance(request.payload, dict) else {}
            message = self._build_slash_message(payload)
            if message is not None:
                self._schedule_command_message(message)
        elif request.type == "interactive":
            payload = request.payload if isinstance(request.payload, dict) else {}
            message = self._build_interaction_message(payload)
            if message is not None:
                self._schedule_command_message(message)

    def _schedule_inbound_message(self, message: ChannelInboundMessage) -> None:
        if self._message_callback is None:
            return
        asyncio.create_task(self._message_callback(message))

    def _schedule_command_message(self, message: ChannelInboundMessage) -> None:
        router = self._router
        binding = self._command_binding()
        if router is not None and binding is not None and hasattr(router, "dispatch_command"):
            asyncio.create_task(self._dispatch_command(router, message, binding))
            return
        self._schedule_inbound_message(message)

    async def _dispatch_command(self, router: Any, message: ChannelInboundMessage, binding: ChannelBinding) -> None:
        try:
            content = await router.dispatch_command(message, binding=binding)
        except Exception:
            logger.exception("[SlackIMAdapter] Slack command dispatch failed")
            return
        if content is None:
            return
        try:
            await self.reply(message, content)
        except Exception:
            logger.exception("[SlackIMAdapter] Slack command reply failed")

    def _command_binding(self) -> ChannelBinding | None:
        for binding in self._bindings:
            if binding.channel_type == self.channel_type:
                return binding
        return self._bindings[0] if self._bindings else None

    def _verify_signature(self, body: bytes, headers: Any) -> bool:
        signing_secret = str(self._client._config.signing_secret or "").strip()
        if not signing_secret:
            return False
        timestamp = str(headers.get("X-Slack-Request-Timestamp", "")).strip()
        signature = str(headers.get("X-Slack-Signature", "")).strip()
        try:
            request_ts = int(timestamp)
        except ValueError:
            return False
        if abs(int(time.time()) - request_ts) > 300:
            return False
        base = b"v0:" + timestamp.encode("utf-8") + b":" + body
        digest = hmac.new(
            signing_secret.encode("utf-8"),
            base,
            hashlib.sha256,
        ).hexdigest()
        expected = f"v0={digest}"
        return bool(signature) and hmac.compare_digest(expected, signature)

    def _parse_form_payload(self, body: bytes) -> dict[str, Any]:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        if "payload" in parsed and parsed["payload"]:
            try:
                payload = json.loads(parsed["payload"][0])
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                return payload
        return {
            key: values[0] if values else ""
            for key, values in parsed.items()
        }

    def _build_slash_message(self, payload: dict[str, Any]) -> ChannelInboundMessage | None:
        command = str(payload.get("command", "")).strip()
        trigger_id = str(payload.get("trigger_id", "")).strip()
        team_id = str(payload.get("team_id", "") or self._client._config.team_id).strip()
        if not command or not trigger_id:
            return None
        text = str(payload.get("text", "")).strip()
        return ChannelInboundMessage(
            message_id=f"slack-slash:{team_id}:{trigger_id}",
            channel_type=self.channel_type,
            chat_id=str(payload.get("channel_id", "")).strip(),
            chat_type="p2p",
            sender_id=str(payload.get("user_id", "")).strip(),
            sender_name=str(payload.get("user_name", "")).strip(),
            content=f"{command} {text}".strip(),
            msg_type="command",
            raw_event=freeze_data(payload),
            metadata=freeze_data({
                "response_url": str(payload.get("response_url", "")).strip(),
                "slack_entry": "slash",
                "team_id": team_id,
                "channel_id": str(payload.get("channel_id", "")).strip(),
            }),
        )

    def _build_interaction_message(self, payload: dict[str, Any]) -> ChannelInboundMessage | None:
        actions = payload.get("actions") or []
        if not isinstance(actions, list) or not actions:
            return None
        action = actions[0] if isinstance(actions[0], dict) else {}
        action_id = str(action.get("action_id", "")).strip()
        action_ts = str(payload.get("action_ts", "")).strip()
        value = str(action.get("value", "")).strip()
        container = payload.get("container", {}) if isinstance(payload.get("container"), dict) else {}
        user = payload.get("user", {}) if isinstance(payload.get("user"), dict) else {}
        team = payload.get("team", {}) if isinstance(payload.get("team"), dict) else {}
        channel = payload.get("channel", {}) if isinstance(payload.get("channel"), dict) else {}
        if not action_id or not action_ts:
            return None
        return ChannelInboundMessage(
            message_id=f"slack-action:{action_ts}:{action_id}",
            channel_type=self.channel_type,
            chat_id=str(channel.get("id", container.get("channel_id", ""))).strip(),
            chat_type="group",
            sender_id=str(user.get("id", "")).strip(),
            sender_name=str(user.get("username", user.get("name", ""))).strip(),
            content=f"/{action_id} {value}".strip(),
            msg_type="interaction",
            reply_to_id=str(container.get("thread_ts", container.get("message_ts", ""))).strip() or None,
            raw_event=freeze_data(payload),
            metadata=freeze_data({
                "response_url": str(payload.get("response_url", "")).strip(),
                "slack_entry": "block_actions",
                "team_id": str(team.get("id", self._client._config.team_id)).strip(),
            }),
        )

    async def _resolve_user_name(self, user_id: str) -> str:
        if not user_id:
            return ""
        try:
            response = await self._client.users_info(user_id)
        except Exception:
            return user_id
        user = response.get("user", {}) if isinstance(response, dict) else {}
        if not isinstance(user, dict):
            return user_id
        profile = user.get("profile", {}) if isinstance(user.get("profile"), dict) else {}
        return (
            str(profile.get("real_name_normalized", ""))
            or str(profile.get("real_name", ""))
            or str(user.get("real_name", ""))
            or user_id
        )

    async def _post_response_url(self, url: str, content: ReplyContent) -> str:
        payload: dict[str, Any] = {"response_type": "in_channel"}
        if content.content_type == "card":
            payload["blocks"] = content.card_dict.get("blocks", [])
        else:
            payload["text"] = content.text
        await self._client.post_response_url(url, payload)
        return f"slack-response-url:{hash(url)}"

    async def _sleep_after_failure(self) -> None:
        delay = self._reconnect.snapshot().get("current_backoff_seconds", 0.0) or 0.0
        if delay > 0:
            await asyncio.sleep(float(delay))

    def _build_reconnect_controller(self) -> ReconnectController:
        features = self._bindings[0].features_dict if self._bindings else {}
        return build_reconnect_controller(features)

    def _update_interval_seconds(self) -> float:
        features = self._bindings[0].features_dict if self._bindings else {}
        raw = features.get(
            "update_interval_ms",
            os.getenv("SLACK_CHAT_UPDATE_MIN_INTERVAL_MS", "500"),
        )
        try:
            milliseconds = int(raw)
        except (TypeError, ValueError):
            milliseconds = 500
        return max(milliseconds, 0) / 1000.0

    def _startup_probe_seconds(self) -> float:
        features = self._bindings[0].features_dict if self._bindings else {}
        raw = features.get(
            "startup_probe_seconds",
            os.getenv("SLACK_SOCKET_MODE_STARTUP_PROBE_SECONDS", "0.1"),
        )
        try:
            return max(float(raw), 0.0)
        except (TypeError, ValueError):
            return 0.1

    def _apply_mode_degradation(self) -> None:
        if "socket_mode" in self._declared_modes and not _socket_mode_sdk_available():
            self._degraded_modes["socket_mode"] = "missing_optional_sdk:slack_sdk"
        if "socket_mode" in self._declared_modes and not self._client._config.app_token:
            self._degraded_modes["socket_mode"] = "missing_slack_app_token"
        if "webhook" in self._declared_modes and not self._client._config.signing_secret:
            self._degraded_modes["webhook"] = "missing_slack_signing_secret"

    def _sync_degraded_reason(self) -> None:
        self._degraded_reason = next(iter(self._degraded_modes.values()), "")

    async def _safe_disconnect(self, client: SocketModeClient | None) -> None:
        if client is None:
            return
        close = getattr(client, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result
            return
        disconnect = getattr(client, "disconnect", None)
        if callable(disconnect):
            result = disconnect()
            if inspect.isawaitable(result):
                await result

    def _normalize_text(self, text: str) -> str:
        text = _MENTION_RE.sub(lambda match: f"@{match.group(1)}", text)
        text = _CHANNEL_RE.sub(lambda match: f"#{match.group(2) or match.group(1)}", text)
        text = _SUBTEAM_RE.sub(
            lambda match: f"@{str(match.group(2) or match.group(1)).lstrip('@')}",
            text,
        )
        return text


def _normalize_chat_type(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "im":
        return "p2p"
    if normalized in {"mpim", "group"}:
        return "group"
    if normalized == "channel":
        return "channel"
    return normalized or "group"


def _extract_ts(response: dict[str, Any]) -> str:
    return str(response.get("ts", response.get("message", {}).get("ts", "")))


def _socket_mode_sdk_available() -> bool:
    if _SLACK_SOCKET_MODE_SDK_AVAILABLE:
        return True
    return SocketModeClient.__module__ != __name__
