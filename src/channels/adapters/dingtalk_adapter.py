"""DingTalk IM adapter implementation."""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, AsyncGenerator

from src.common.logger import get_logger
from src.services.dingtalk.client import DingTalkClient

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
from ..models import ChannelBinding, ChannelInboundMessage, ReplyContent, StreamChunk, freeze_data
from ..protocol import MessageCallback

logger = get_logger()


class DingTalkIMAdapter:
    """DingTalk IM adapter with stream/webhook compatible parsing."""

    channel_type = "dingtalk"

    def __init__(
        self,
        client: DingTalkClient,
        bindings: tuple[ChannelBinding, ...],
    ) -> None:
        self._client = client
        self._bindings = bindings
        self._message_callback: MessageCallback | None = None
        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream_task: asyncio.Task[Any] | None = None
        self._stream_client: Any | None = None
        self._connection_state = "stopped"
        self._last_error = ""
        self._last_connected_at = ""
        self._last_event_at = ""
        self._last_disconnect_at = ""
        self._degraded_reason = ""
        self._stream_bindings = tuple(
            binding for binding in bindings
            if binding.connection_mode == "stream"
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
        await self._start_stream_client()

    async def stop(self) -> None:
        self._started = False
        self._connection_state = "stopped"
        await self._stop_stream_client()

    async def health_check(self) -> bool:
        if not self._started:
            return False
        if self._stream_bindings:
            if self._connection_state == "connected":
                return True
            return any(binding.connection_mode != "stream" for binding in self._bindings)
        return True

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "channel_type": self.channel_type,
            "started": self._started,
            "healthy": self._started and (self._connection_state == "connected" or not self._stream_bindings),
            "connection_state": self._connection_state,
            "active_modes": sorted({binding.connection_mode for binding in self._bindings}),
            "stream_enabled": bool(self._stream_bindings),
            "stream_running": is_task_running(self._stream_task) or _client_running(self._stream_client),
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
        chat_id = str(raw_event.get("conversationId", raw_event.get("conversation_id", ""))).strip()
        if self._chat_ids and chat_id not in self._chat_ids:
            return None
        text_payload = raw_event.get("text", {}) or {}
        content = str(text_payload.get("content", raw_event.get("content", "")))
        msg_type = str(raw_event.get("msgtype", raw_event.get("msg_type", "text"))).strip().lower() or "text"
        return ChannelInboundMessage(
            message_id=str(raw_event.get("msgId", raw_event.get("msg_id", ""))),
            channel_type=self.channel_type,
            chat_id=chat_id,
            chat_type="group" if chat_id else "p2p",
            sender_id=str(raw_event.get("senderId", raw_event.get("sender_id", ""))),
            sender_name=str(raw_event.get("senderNick", raw_event.get("sender_nick", ""))),
            content=content,
            msg_type=msg_type,
            reply_to_id=str(raw_event.get("sessionWebhookExpiredTime", "")) or None,
            raw_event=freeze_data(raw_event),
            metadata=freeze_data({
                "robot_code": raw_event.get("robotCode", ""),
            }),
        )

    async def reply(
        self,
        source_msg: ChannelInboundMessage,
        content: ReplyContent,
    ) -> str:
        if content.content_type == "card":
            response = await self._client.send_action_card(source_msg.chat_id, content.card_dict)
        else:
            response = await self._client.reply_message(
                source_msg.chat_id,
                content.text,
                msg_type="text",
            )
        return _extract_message_id(response)

    async def reply_stream(
        self,
        source_msg: ChannelInboundMessage,
        chunks: AsyncGenerator[StreamChunk, None],
    ) -> str:
        initial_card = _build_stream_card("思考中...", finished=False)
        if hasattr(self._client, "send_interactive_card"):
            response = await self._client.send_interactive_card(
                source_msg.chat_id,
                initial_card,
            )
        else:
            response = await self._client.send_action_card(
                source_msg.chat_id,
                initial_card,
            )
        card_instance_id = _extract_message_id(response)
        accumulated = ""

        async for chunk in chunks:
            if chunk.chunk_type == "text_delta":
                accumulated += chunk.content
                if hasattr(self._client, "update_card") and card_instance_id:
                    await self._client.update_card(
                        card_instance_id,
                        _build_stream_card(accumulated or "思考中...", finished=False),
                    )
            elif chunk.chunk_type == "finished":
                break

        if hasattr(self._client, "update_card") and card_instance_id:
            await self._client.update_card(
                card_instance_id,
                _build_stream_card(accumulated or "已完成", finished=True),
            )
            return card_instance_id
        if accumulated:
            return await self.reply(source_msg, ReplyContent(text=accumulated))
        return card_instance_id

    async def send_message(
        self,
        chat_id: str,
        content: ReplyContent,
    ) -> str:
        if content.content_type == "card":
            response = await self._client.send_action_card(chat_id, content.card_dict)
        else:
            response = await self._client.reply_message(chat_id, content.text, msg_type="text")
        return _extract_message_id(response)

    async def handle_webhook(self, request: Any) -> Any:
        payload = await request.json()
        challenge = str(payload.get("challenge", "")).strip()
        if challenge:
            return {"challenge": challenge}
        message = await self.parse_event(payload)
        if message is not None and self._message_callback is not None:
            await self._message_callback(message)
        return {"status": "ok"}

    async def _start_stream_client(self) -> None:
        if not self._stream_bindings or is_task_running(self._stream_task):
            return

        sdk = optional_import("dingtalk_stream")
        if sdk is None:
            logger.warning("[DingTalkIMAdapter] dingtalk_stream not installed; stream mode disabled")
            self._connection_state = "degraded"
            self._degraded_reason = "missing_optional_sdk:dingtalk_stream"
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
                    self._stream_client = self._build_stream_client(sdk)
                    if self._stream_client is None:
                        self._connection_state = "degraded"
                        self._degraded_reason = "unsupported_sdk_client"
                        logger.warning("[DingTalkIMAdapter] No compatible stream client entry found in dingtalk_stream")
                        return
                    runner = _select_dingtalk_stream_runner(self._stream_client)
                    if runner is None:
                        self._connection_state = "degraded"
                        self._degraded_reason = "unsupported_sdk_runner"
                        logger.warning("[DingTalkIMAdapter] No compatible stream start method found")
                        self._stream_client = None
                        return
                    run_task = asyncio.create_task(runner())
                    await asyncio.sleep(self._startup_probe_seconds())
                    if run_task.done():
                        await run_task
                        if not self._started:
                            break
                        self._reconnect.register_failure(RuntimeError("stream connection closed"))
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
                    self._reconnect.register_failure(RuntimeError("stream connection closed"))
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
                    logger.exception("[DingTalkIMAdapter] Stream loop exited unexpectedly")
                    await self._sleep_after_failure()
                finally:
                    self._stream_client = None

            if self._started and self._stream_bindings:
                self._connection_state = "degraded"
            elif not self._started:
                self._connection_state = "stopped"

        self._stream_task = asyncio.create_task(
            _run_supervisor(),
            name="dingtalk-im-stream",
        )

    async def _stop_stream_client(self) -> None:
        client = self._stream_client
        self._stream_client = None
        for stop_name in ("stop", "close", "shutdown"):
            stop_method = getattr(client, stop_name, None)
            if callable(stop_method):
                try:
                    await call_maybe_async(stop_method)
                except Exception:
                    logger.exception("[DingTalkIMAdapter] Failed to stop stream client")
                break
        await cancel_task(self._stream_task)
        self._stream_task = None

    def _build_stream_client(self, sdk: Any) -> Any | None:
        credential_cls = getattr(sdk, "Credential", None)
        stream_client_cls = getattr(sdk, "DingTalkStreamClient", None) or getattr(sdk, "Client", None)
        if credential_cls is None or stream_client_cls is None:
            return None

        credential = call_with_variants(
            credential_cls,
            build_variant_candidates(
                ((
                    getattr(self._client._config, "app_key", ""),
                    getattr(self._client._config, "app_secret", ""),
                ), {}),
                ((), {
                    "client_id": getattr(self._client._config, "app_key", ""),
                    "client_secret": getattr(self._client._config, "app_secret", ""),
                }),
                ((), {
                    "app_key": getattr(self._client._config, "app_key", ""),
                    "app_secret": getattr(self._client._config, "app_secret", ""),
                }),
            ),
        )
        client = call_with_variants(
            stream_client_cls,
            build_variant_candidates(
                ((credential,), {}),
                ((), {"credential": credential}),
            ),
        )

        handler = _build_dingtalk_handler(self, sdk)
        register = getattr(client, "register_callback_handler", None) or getattr(client, "register_handler", None)
        if callable(register):
            topic = _resolve_dingtalk_topic(sdk, self._stream_bindings)
            call_with_variants(
                register,
                build_variant_candidates(
                    ((topic, handler), {}),
                    ((handler,), {}),
                ),
            )
        return client

    def _schedule_stream_event(self, payload: Any) -> None:
        future = submit_coroutine(self._loop, self._consume_stream_event(payload))
        if future is not None:
            future.add_done_callback(_log_sdk_future_error)

    async def _consume_stream_event(self, payload: Any) -> None:
        raw_event = _normalize_dingtalk_event(payload)
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
        for binding in self._stream_bindings:
            features.update(binding.features_dict)
        return build_reconnect_controller(features)

    def _sync_reconnect_state(self) -> None:
        snapshot = self._reconnect.snapshot()
        self._last_error = str(snapshot.get("last_error", ""))

    def _startup_probe_seconds(self) -> float:
        features: dict[str, Any] = {}
        for binding in self._stream_bindings:
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


async def _collect_text(chunks: AsyncGenerator[StreamChunk, None]) -> str:
    parts: list[str] = []
    async for chunk in chunks:
        if chunk.chunk_type == "text_delta":
            parts.append(chunk.content)
    return "".join(parts)


def _extract_message_id(response: dict[str, Any]) -> str:
    return str(
        response.get("processQueryKey")
        or response.get("messageId")
        or response.get("request_id", "")
    )


def _build_stream_card(content: str, *, finished: bool) -> dict[str, Any]:
    return {
        "title": "助手回复",
        "markdown": content,
        "status": "finished" if finished else "streaming",
    }


def _build_dingtalk_handler(adapter: DingTalkIMAdapter, sdk: Any) -> Any:
    base_cls = getattr(sdk, "ChatbotHandler", object)
    ack_message = getattr(sdk, "AckMessage", None)

    class _AdapterHandler(base_cls):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            init = getattr(super(), "__init__", None)
            if callable(init):
                try:
                    init()
                except TypeError:
                    pass

        async def process(self, callback: Any) -> Any:
            adapter._schedule_stream_event(callback)
            if ack_message is not None:
                status = getattr(ack_message, "STATUS_OK", "OK")
                return status, "OK"
            return None

    return _AdapterHandler()


def _resolve_dingtalk_topic(sdk: Any, bindings: tuple[ChannelBinding, ...]) -> str:
    features = bindings[0].features_dict if bindings else {}
    if str(features.get("topic", "")).strip():
        return str(features["topic"])
    chatbot_message = getattr(sdk, "ChatbotMessage", None)
    topic = getattr(chatbot_message, "TOPIC", "")
    return str(topic or "chatbot.message")


def _select_dingtalk_stream_runner(client: Any) -> Any | None:
    for method_name in ("start", "start_forever", "run_forever", "run"):
        method = getattr(client, method_name, None)
        if not callable(method):
            continue

        async def _runner(method: Any = method) -> None:
            if inspect.iscoroutinefunction(method):
                result = call_with_variants(method, build_variant_candidates(((), {}),))
                if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                    await result
                return
            await asyncio.to_thread(call_with_variants, method, build_variant_candidates(((), {}),))

        return _runner
    return None


def _normalize_dingtalk_event(payload: Any) -> dict[str, Any]:
    raw_event = payload
    if hasattr(payload, "data"):
        raw_event = getattr(payload, "data")
    raw_event = object_to_dict(raw_event)
    if isinstance(raw_event, dict) and "data" in raw_event and "conversationId" not in raw_event:
        inner = raw_event.get("data")
        if isinstance(inner, dict):
            raw_event = inner
    return raw_event if isinstance(raw_event, dict) else {}


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
        logger.exception("[DingTalkIMAdapter] Failed to inspect sdk callback future")
        return
    if error is not None:
        logger.exception("[DingTalkIMAdapter] SDK callback failed", exc_info=error)
