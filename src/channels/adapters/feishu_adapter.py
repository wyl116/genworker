"""Feishu IM adapter implementation."""
from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, AsyncGenerator

from src.common.logger import get_logger
from src.services.feishu.client import FeishuClient

from ._sdk_runtime import (
    ReconnectController,
    build_variant_candidates,
    build_reconnect_controller,
    call_maybe_async,
    call_with_variants,
    cancel_task,
    is_task_running,
    object_to_dict,
    optional_import,
    submit_coroutine,
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


class FeishuIMAdapter:
    """Feishu IM adapter with webhook-first phase-1 support."""

    channel_type = "feishu"

    def __init__(
        self,
        client: FeishuClient,
        bindings: tuple[ChannelBinding, ...],
    ) -> None:
        self._client = client
        self._bindings = bindings
        self._message_callback: MessageCallback | None = None
        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._websocket_task: asyncio.Task[Any] | None = None
        self._websocket_client: Any | None = None
        self._connection_state = "stopped"
        self._last_error = ""
        self._last_connected_at = ""
        self._last_event_at = ""
        self._last_disconnect_at = ""
        self._degraded_reason = ""
        self._websocket_bindings = tuple(
            binding for binding in bindings
            if binding.connection_mode == "websocket"
        )
        self._chat_ids = {
            chat_id
            for binding in bindings
            for chat_id in binding.chat_ids
            if chat_id
        }
        self._reconnect = self._build_reconnect_controller()

    def supports_streaming(self) -> bool:
        return True

    async def start(self, message_callback: MessageCallback) -> None:
        self._message_callback = message_callback
        self._started = True
        self._loop = asyncio.get_running_loop()
        self._connection_state = "starting"
        self._degraded_reason = ""
        self._reconnect.reset()
        self._sync_reconnect_state()
        await self._start_websocket_client()

    async def stop(self) -> None:
        self._started = False
        self._connection_state = "stopped"
        await self._stop_websocket_client()

    async def health_check(self) -> bool:
        if not self._started:
            return False
        if self._websocket_bindings:
            if self._connection_state == "connected":
                return True
            return any(binding.connection_mode != "websocket" for binding in self._bindings)
        return True

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "channel_type": self.channel_type,
            "started": self._started,
            "healthy": self._started and (self._connection_state == "connected" or not self._websocket_bindings),
            "connection_state": self._connection_state,
            "active_modes": sorted({binding.connection_mode for binding in self._bindings}),
            "websocket_enabled": bool(self._websocket_bindings),
            "websocket_running": is_task_running(self._websocket_task) or _client_running(self._websocket_client),
            "last_error": self._last_error,
            "last_connected_at": self._last_connected_at,
            "last_event_at": self._last_event_at,
            "last_disconnect_at": self._last_disconnect_at,
            "degraded_reason": self._degraded_reason,
            **self._reconnect.snapshot(),
        }

    async def parse_event(self, raw_event: Any) -> ChannelInboundMessage | None:
        if not isinstance(raw_event, dict):
            return None
        header = raw_event.get("header", {}) or {}
        if str(header.get("event_type", "")).strip() != "im.message.receive_v1":
            return None

        event = raw_event.get("event", {}) or {}
        message = event.get("message", {}) or {}
        sender = event.get("sender", {}) or {}
        sender_id = sender.get("sender_id", {}) or {}
        chat_id = str(message.get("chat_id", "")).strip()
        if self._chat_ids and chat_id not in self._chat_ids:
            return None
        if str(sender.get("sender_type", "")).lower() == "bot":
            return None

        content_payload = _parse_feishu_content(message.get("content", ""), message.get("message_type", "text"))
        mentions = tuple(
            Mention(
                user_id=str(item.get("id", "") or item.get("open_id", "") or item.get("user_id", "")),
                name=str(item.get("name", item.get("key", ""))),
                is_bot=bool(item.get("id_type") == "app_id" or item.get("name") == "bot"),
            )
            for item in content_payload.get("mentions", ())
            if isinstance(item, dict)
        )
        attachments = tuple(
            Attachment(
                file_key=str(item.get("file_key", item.get("image_key", ""))),
                file_name=str(item.get("file_name", item.get("name", ""))),
                file_type=str(item.get("file_type", message.get("message_type", "file"))),
                size=int(item.get("file_size", item.get("size", 0) or 0)),
            )
            for item in content_payload.get("attachments", ())
            if isinstance(item, dict)
        )
        return ChannelInboundMessage(
            message_id=str(message.get("message_id", "")),
            channel_type=self.channel_type,
            chat_id=chat_id,
            chat_type=str(message.get("chat_type", "group")).strip().lower() or "group",
            sender_id=str(sender_id.get("open_id", sender_id.get("user_id", ""))),
            sender_name=str(sender.get("sender_name", event.get("sender_name", ""))),
            content=str(content_payload.get("text", "")),
            msg_type=str(message.get("message_type", "text")),
            reply_to_id=str(message.get("parent_id", "")) or None,
            mentions=mentions,
            attachments=attachments,
            raw_event=freeze_data(raw_event),
            metadata=freeze_data({
                "message_type": message.get("message_type", "text"),
                "chat_type": message.get("chat_type", "group"),
            }),
        )

    async def reply(
        self,
        source_msg: ChannelInboundMessage,
        content: ReplyContent,
    ) -> str:
        if content.content_type == "card":
            response = await self._client.send_card(source_msg.chat_id, content.card_dict)
        else:
            response = await self._client.reply_message(
                source_msg.message_id,
                content.text,
                msg_type="text",
            )
        return _extract_message_id(response)

    async def reply_stream(
        self,
        source_msg: ChannelInboundMessage,
        chunks: AsyncGenerator[StreamChunk, None],
    ) -> str:
        card_payload = {"config": {"wide_screen_mode": True}, "elements": [{"tag": "markdown", "content": ""}]}
        response = await self._client.send_card(source_msg.chat_id, card_payload)
        message_id = _extract_message_id(response)
        aggregated = ""
        async for chunk in chunks:
            if chunk.chunk_type == "text_delta":
                aggregated += chunk.content
                card_payload["elements"][0]["content"] = aggregated
                await self._client.update_card(message_id, card_payload)
            elif chunk.chunk_type == "finished":
                break
        return message_id

    async def send_message(
        self,
        chat_id: str,
        content: ReplyContent,
    ) -> str:
        if content.content_type == "card":
            response = await self._client.send_card(chat_id, content.card_dict)
        else:
            response = await self._client.send_chat_message(chat_id, content.text)
        return _extract_message_id(response)

    async def handle_webhook(self, request: Any) -> Any:
        payload = await request.json()
        if str(payload.get("type", "")).strip() == "url_verification":
            return {"challenge": payload.get("challenge", "")}
        message = await self.parse_event(payload)
        if message is not None and self._message_callback is not None:
            await self._message_callback(message)
        return {"status": "ok"}

    async def _start_websocket_client(self) -> None:
        if not self._websocket_bindings or is_task_running(self._websocket_task):
            return

        sdk = optional_import("lark_oapi")
        if sdk is None:
            logger.warning("[FeishuIMAdapter] lark_oapi not installed; websocket mode disabled")
            self._connection_state = "degraded"
            self._degraded_reason = "missing_optional_sdk:lark_oapi"
            return

        async def _run_supervisor() -> None:
            while self._started:
                wait_seconds = self._reconnect.before_attempt()
                self._sync_reconnect_state()
                if wait_seconds > 0:
                    self._connection_state = "reconnecting"
                    self._degraded_reason = "circuit_open"
                    await asyncio.sleep(wait_seconds)
                    if not self._started:
                        break
                try:
                    self._connection_state = "connecting"
                    dispatcher = self._build_websocket_dispatcher(sdk)
                    self._websocket_client = _build_feishu_websocket_client(
                        sdk=sdk,
                        app_id=str(getattr(self._client._config, "app_id", "")),
                        app_secret=str(getattr(self._client._config, "app_secret", "")),
                        dispatcher=dispatcher,
                    )
                    if self._websocket_client is None:
                        self._connection_state = "degraded"
                        self._degraded_reason = "unsupported_sdk_client"
                        logger.warning("[FeishuIMAdapter] No compatible websocket client entry found in lark_oapi")
                        return
                    runner = _select_feishu_websocket_runner(self._websocket_client, dispatcher)
                    if runner is None:
                        self._connection_state = "degraded"
                        self._degraded_reason = "unsupported_sdk_runner"
                        logger.warning("[FeishuIMAdapter] No compatible websocket start method found")
                        self._websocket_client = None
                        return
                    run_task = asyncio.create_task(runner())
                    await asyncio.sleep(self._startup_probe_seconds())
                    if run_task.done():
                        await run_task
                        if not self._started:
                            break
                        self._reconnect.register_failure(RuntimeError("websocket connection closed"))
                        self._sync_reconnect_state()
                        self._last_disconnect_at = utc_now_iso()
                        self._connection_state = "reconnecting" if self._reconnect.breaker_state != "open" else "degraded"
                        self._degraded_reason = "circuit_open" if self._reconnect.breaker_state == "open" else "connection_closed"
                        await self._sleep_after_failure()
                        continue
                    self._reconnect.register_success()
                    self._sync_reconnect_state()
                    self._connection_state = "connected"
                    self._degraded_reason = ""
                    self._last_connected_at = utc_now_iso()
                    await run_task
                    if not self._started:
                        break
                    self._reconnect.register_failure(RuntimeError("websocket connection closed"))
                    self._sync_reconnect_state()
                    self._last_disconnect_at = utc_now_iso()
                    self._connection_state = "reconnecting" if self._reconnect.breaker_state != "open" else "degraded"
                    self._degraded_reason = "circuit_open" if self._reconnect.breaker_state == "open" else "connection_closed"
                    await self._sleep_after_failure()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._reconnect.register_failure(exc)
                    self._sync_reconnect_state()
                    self._last_disconnect_at = utc_now_iso()
                    self._connection_state = "reconnecting" if self._reconnect.breaker_state != "open" else "degraded"
                    self._degraded_reason = "circuit_open" if self._reconnect.breaker_state == "open" else "connection_error"
                    logger.exception("[FeishuIMAdapter] Websocket loop exited unexpectedly")
                    await self._sleep_after_failure()
                finally:
                    self._websocket_client = None

            if self._started and self._websocket_bindings:
                self._connection_state = "degraded"
            elif not self._started:
                self._connection_state = "stopped"

        self._websocket_task = asyncio.create_task(
            _run_supervisor(),
            name="feishu-im-websocket",
        )

    async def _stop_websocket_client(self) -> None:
        client = self._websocket_client
        self._websocket_client = None
        for stop_name in ("stop", "close", "shutdown"):
            stop_method = getattr(client, stop_name, None)
            if callable(stop_method):
                try:
                    await call_maybe_async(stop_method)
                except Exception:
                    logger.exception("[FeishuIMAdapter] Failed to stop websocket client")
                break
        await cancel_task(self._websocket_task)
        self._websocket_task = None

    def _build_websocket_dispatcher(self, sdk: Any) -> Any:
        features = self._websocket_bindings[0].features_dict if self._websocket_bindings else {}
        encrypt_key = str(features.get("encrypt_key", features.get("encryptKey", "")))
        verification_token = str(features.get("verification_token", features.get("verificationToken", "")))

        builder = None
        dispatcher_cls = getattr(sdk, "EventDispatcherHandler", None)
        if dispatcher_cls is not None:
            builder = getattr(dispatcher_cls, "builder", None)
        if builder is None:
            ws_namespace = getattr(sdk, "ws", None)
            dispatcher_cls = getattr(ws_namespace, "EventDispatcherHandler", None)
            if dispatcher_cls is not None:
                builder = getattr(dispatcher_cls, "builder", None)
        if builder is None:
            return self._handle_websocket_event

        dispatcher_builder = call_with_variants(
            builder,
            build_variant_candidates(
                ((encrypt_key, verification_token), {}),
                ((encrypt_key, verification_token, None), {}),
                ((), {}),
            ),
        )

        for register_name in (
            "register_p2_im_message_receive_v1",
            "register_p2_application_bot_message_receive_v1",
        ):
            register = getattr(dispatcher_builder, register_name, None)
            if callable(register):
                register(self._handle_websocket_event)
                break
        else:
            register = getattr(dispatcher_builder, "register", None)
            if callable(register):
                call_with_variants(
                    register,
                    build_variant_candidates(
                        (("im.message.receive_v1", self._handle_websocket_event), {}),
                        ((self._handle_websocket_event,), {}),
                    ),
                )

        build = getattr(dispatcher_builder, "build", None)
        if callable(build):
            return build()
        return dispatcher_builder

    def _handle_websocket_event(self, payload: Any = None, *args: Any, **kwargs: Any) -> None:
        event = payload
        if event is None and args:
            event = args[0]
        if event is None and kwargs:
            event = kwargs.get("event") or kwargs.get("data") or kwargs
        future = submit_coroutine(self._loop, self._consume_websocket_event(event))
        if future is not None:
            future.add_done_callback(_log_sdk_future_error)

    async def _consume_websocket_event(self, payload: Any) -> None:
        raw_event = _normalize_feishu_event(payload)
        message = await self.parse_event(raw_event)
        if message is not None and self._message_callback is not None:
            if self._connection_state != "connected":
                self._reconnect.register_success()
                self._sync_reconnect_state()
                self._connection_state = "connected"
                self._degraded_reason = ""
                if not self._last_connected_at:
                    self._last_connected_at = utc_now_iso()
            self._last_event_at = utc_now_iso()
            await self._message_callback(message)

    def _build_reconnect_controller(self) -> ReconnectController:
        features: dict[str, Any] = {}
        for binding in self._websocket_bindings:
            features.update(binding.features_dict)
        return build_reconnect_controller(features)

    def _sync_reconnect_state(self) -> None:
        snapshot = self._reconnect.snapshot()
        self._last_error = str(snapshot.get("last_error", ""))

    def _startup_probe_seconds(self) -> float:
        features: dict[str, Any] = {}
        for binding in self._websocket_bindings:
            features.update(binding.features_dict)
        value = features.get("startup_probe_seconds", 0.05)
        try:
            return max(float(value), 0.01)
        except (TypeError, ValueError):
            return 0.05

    async def _sleep_after_failure(self) -> None:
        delay = float(self._reconnect.snapshot().get("current_backoff_seconds", 0.0) or 0.0)
        if self._started and delay > 0:
            await asyncio.sleep(delay)


def _parse_feishu_content(raw_content: Any, message_type: str) -> dict[str, Any]:
    payload: dict[str, Any]
    if isinstance(raw_content, dict):
        payload = dict(raw_content)
    else:
        try:
            payload = json.loads(str(raw_content or "{}"))
        except json.JSONDecodeError:
            payload = {"text": str(raw_content or "")}

    if message_type == "text" and "text" not in payload:
        payload["text"] = str(raw_content or "")
    if message_type in {"image", "file"}:
        payload.setdefault("attachments", [payload])
    return payload


def _extract_message_id(response: dict[str, Any]) -> str:
    data = response.get("data", {}) if isinstance(response, dict) else {}
    if isinstance(data, dict):
        return str(data.get("message_id", ""))
    return ""


def _build_feishu_websocket_client(
    *,
    sdk: Any,
    app_id: str,
    app_secret: str,
    dispatcher: Any,
) -> Any | None:
    ws_namespace = getattr(sdk, "ws", None)
    client_cls = getattr(ws_namespace, "Client", None) or getattr(sdk, "WSClient", None)
    if client_cls is None:
        return None

    api_client = _build_feishu_openapi_client(sdk, app_id, app_secret)
    return call_with_variants(
        client_cls,
        build_variant_candidates(
            ((api_client,), {"event_handler": dispatcher}),
            ((api_client,), {"handler": dispatcher}),
            ((api_client,), {"dispatcher": dispatcher}),
            ((app_id, app_secret), {"event_handler": dispatcher}),
            ((app_id, app_secret), {"handler": dispatcher}),
            ((app_id, app_secret, dispatcher), {}),
            ((), {"app_id": app_id, "app_secret": app_secret, "event_handler": dispatcher}),
        ),
    )


def _build_feishu_openapi_client(sdk: Any, app_id: str, app_secret: str) -> Any:
    client_cls = getattr(sdk, "Client", None)
    builder = getattr(client_cls, "builder", None)
    if callable(builder):
        builder_obj = builder()
        for name, value in (("app_id", app_id), ("app_secret", app_secret)):
            setter = getattr(builder_obj, name, None)
            if callable(setter):
                builder_obj = setter(value)
        build = getattr(builder_obj, "build", None)
        if callable(build):
            return build()
    return {"app_id": app_id, "app_secret": app_secret}


def _select_feishu_websocket_runner(client: Any, dispatcher: Any) -> Any | None:
    for method_name in ("start", "start_forever", "run_forever", "run"):
        method = getattr(client, method_name, None)
        if not callable(method):
            continue

        async def _runner(method: Any = method) -> None:
            if inspect.iscoroutinefunction(method):
                result = call_with_variants(
                    method,
                    build_variant_candidates(
                        ((), {}),
                        ((dispatcher,), {}),
                        ((), {"event_handler": dispatcher}),
                        ((), {"handler": dispatcher}),
                        ((), {"dispatcher": dispatcher}),
                    ),
                )
                if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                    await result
                return
            await asyncio.to_thread(
                call_with_variants,
                method,
                build_variant_candidates(
                    ((), {}),
                    ((dispatcher,), {}),
                    ((), {"event_handler": dispatcher}),
                    ((), {"handler": dispatcher}),
                    ((), {"dispatcher": dispatcher}),
                    (({"eventDispatcher": dispatcher},), {}),
                ),
            )

        return _runner
    return None


def _normalize_feishu_event(payload: Any) -> dict[str, Any]:
    raw_event = object_to_dict(payload)
    if not isinstance(raw_event, dict):
        return {}
    if "header" in raw_event and "event" in raw_event:
        return raw_event
    if "event" in raw_event:
        return {
            "header": {"event_type": str(raw_event.get("event_type", "im.message.receive_v1"))},
            "event": raw_event["event"],
        }
    if "message" in raw_event or "sender" in raw_event:
        return {
            "header": {"event_type": "im.message.receive_v1"},
            "event": raw_event,
        }
    return raw_event


def _client_running(client: Any) -> bool:
    if client is None:
        return False
    for attr_name in ("running", "started", "is_running"):
        attr = getattr(client, attr_name, None)
        if callable(attr):
            try:
                attr = attr()
            except TypeError:
                continue
        if bool(attr):
            return True
    return False




def _log_sdk_future_error(future: Any) -> None:
    if future is None or getattr(future, "cancelled", lambda: False)():
        return
    exception_getter = getattr(future, "exception", None)
    if not callable(exception_getter):
        return
    try:
        error = exception_getter()
    except Exception:
        logger.exception("[FeishuIMAdapter] Failed to inspect sdk callback future")
        return
    if error is not None:
        logger.exception("[FeishuIMAdapter] SDK callback failed", exc_info=error)
