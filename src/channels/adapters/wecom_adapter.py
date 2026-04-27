"""WeCom IM adapter implementation."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import subprocess
from struct import unpack
import xml.etree.ElementTree as ET
from typing import Any, AsyncGenerator

from src.common.logger import get_logger
from src.services.wecom.client import WeComClient

from ._sdk_runtime import utc_now_iso
from ..models import ChannelBinding, ChannelInboundMessage, ReplyContent, StreamChunk, freeze_data
from ..protocol import MessageCallback

logger = get_logger()


class WeComIMAdapter:
    """WeCom IM adapter with webhook-based inbound handling."""

    channel_type = "wecom"

    def __init__(
        self,
        client: WeComClient,
        bindings: tuple[ChannelBinding, ...],
    ) -> None:
        self._client = client
        self._bindings = bindings
        self._message_callback: MessageCallback | None = None
        self._started = False
        self._chat_ids = {
            chat_id
            for binding in bindings
            for chat_id in binding.chat_ids
            if chat_id
        }
        self._features = _merge_features(bindings)
        self._last_event_at = ""

    def supports_streaming(self) -> bool:
        return True

    async def start(self, message_callback: MessageCallback) -> None:
        self._message_callback = message_callback
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def health_check(self) -> bool:
        return self._started

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "channel_type": self.channel_type,
            "started": self._started,
            "healthy": self._started,
            "connection_state": "connected" if self._started else "stopped",
            "active_modes": sorted({binding.connection_mode for binding in self._bindings}),
            "webhook_enabled": True,
            "encryption_enabled": self._has_encryption(),
            "last_event_at": self._last_event_at,
        }

    async def parse_event(self, raw_event: Any) -> ChannelInboundMessage | None:
        root = _coerce_xml(raw_event)
        if root is None:
            return None
        msg_type = _xml_text(root, "MsgType").lower()
        chat_id = _xml_text(root, "ConversationId") or _xml_text(root, "ChatId") or _xml_text(root, "FromUserName")
        if self._chat_ids and chat_id not in self._chat_ids:
            return None
        return ChannelInboundMessage(
            message_id=_xml_text(root, "MsgId") or _xml_text(root, "MsgId64"),
            channel_type=self.channel_type,
            chat_id=chat_id,
            chat_type=_resolve_wecom_chat_type(root),
            sender_id=_xml_text(root, "FromUserName"),
            sender_name=_xml_text(root, "FromUserName"),
            content=_xml_text(root, "Content"),
            msg_type=msg_type or "text",
            reply_to_id=_xml_text(root, "SessionId") or None,
            raw_event=freeze_data({"xml": ET.tostring(root, encoding="unicode")}),
            metadata=freeze_data({
                "agent_id": _xml_text(root, "AgentID"),
                "to_user_name": _xml_text(root, "ToUserName"),
            }),
        )

    async def reply(
        self,
        source_msg: ChannelInboundMessage,
        content: ReplyContent,
    ) -> str:
        if content.content_type == "markdown":
            response = await self._client.send_markdown(source_msg.chat_id, content.text)
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
        stream_interval = _stream_interval_seconds(self._features)
        accumulated = ""
        last_sent = ""
        last_emit_at = 0.0
        loop = asyncio.get_running_loop()

        async for chunk in chunks:
            if chunk.chunk_type == "text_delta":
                accumulated += chunk.content
                now = loop.time()
                if accumulated and accumulated != last_sent and (
                    last_emit_at == 0.0 or now - last_emit_at >= stream_interval
                ):
                    await self._client.send_markdown(source_msg.chat_id, accumulated)
                    last_sent = accumulated
                    last_emit_at = now
            elif chunk.chunk_type == "finished":
                break

        if accumulated and accumulated != last_sent:
            response = await self._client.send_markdown(
                source_msg.chat_id,
                accumulated,
            )
            return _extract_message_id(response)
        if accumulated:
            return "stream-sent"
        return await self.reply(source_msg, ReplyContent(text=""))

    async def send_message(
        self,
        chat_id: str,
        content: ReplyContent,
    ) -> str:
        if content.content_type == "markdown":
            response = await self._client.send_markdown(chat_id, content.text)
        else:
            response = await self._client.reply_message(chat_id, content.text, msg_type="text")
        return _extract_message_id(response)

    async def handle_webhook(self, request: Any) -> Any:
        query = dict(request.query_params)
        if request.method.upper() == "GET":
            echostr = query.get("echostr", "")
            if not self._signature_valid(query, echostr):
                return {"status": "invalid_signature"}
            if self._has_encryption() and echostr:
                return {"echostr": self._decrypt_payload(echostr)}
            return {"echostr": echostr}

        body = await request.body()
        payload = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
        signed_value = _extract_encrypted_value(payload) if self._has_encryption() else payload
        if signed_value and not self._signature_valid(query, signed_value):
            logger.warning("[WeComIMAdapter] Signature validation failed")
            return {"status": "invalid_signature"}
        if self._has_encryption():
            encrypted = _extract_encrypted_value(payload)
            if not encrypted:
                return {"status": "invalid_payload"}
            payload = self._decrypt_payload(encrypted)
        message = await self.parse_event(payload)
        if message is not None and self._message_callback is not None:
            self._last_event_at = utc_now_iso()
            await self._message_callback(message)
        return {"status": "ok"}

    def _signature_valid(self, query: dict[str, Any], body: str) -> bool:
        token = str(self._features.get("callback_token", "")).strip()
        if not token:
            return True
        signature = str(query.get("msg_signature", "")).strip()
        timestamp = str(query.get("timestamp", "")).strip()
        nonce = str(query.get("nonce", "")).strip()
        candidate = hashlib.sha1("".join(sorted([token, timestamp, nonce, body])).encode("utf-8")).hexdigest()
        return not signature or candidate == signature

    def _has_encryption(self) -> bool:
        return bool(str(self._features.get("encoding_aes_key", "")).strip())

    def _decrypt_payload(self, encrypted: str) -> str:
        corp_id = getattr(getattr(self._client, "_config", None), "corpid", "") or ""
        return _decrypt_wecom_message(
            encrypted=encrypted,
            encoding_aes_key=str(self._features.get("encoding_aes_key", "")).strip(),
            corp_id=str(corp_id),
        )


def _coerce_xml(raw_event: Any) -> ET.Element | None:
    if isinstance(raw_event, ET.Element):
        return raw_event
    if isinstance(raw_event, dict) and "xml" in raw_event:
        raw_event = raw_event["xml"]
    try:
        return ET.fromstring(str(raw_event or "").strip())
    except ET.ParseError:
        return None


def _xml_text(root: ET.Element, tag: str) -> str:
    node = root.find(tag)
    return (node.text or "").strip() if node is not None and node.text is not None else ""


def _resolve_wecom_chat_type(root: ET.Element) -> str:
    chat_id = _xml_text(root, "ConversationId") or _xml_text(root, "ChatId")
    return "group" if chat_id else "p2p"


def _merge_features(bindings: tuple[ChannelBinding, ...]) -> dict[str, Any]:
    features: dict[str, Any] = {}
    for binding in bindings:
        features.update(binding.features_dict)
    return features


async def _collect_text(chunks: AsyncGenerator[StreamChunk, None]) -> str:
    parts: list[str] = []
    async for chunk in chunks:
        if chunk.chunk_type == "text_delta":
            parts.append(chunk.content)
    return "".join(parts)


def _stream_interval_seconds(features: dict[str, Any]) -> float:
    raw_value = features.get("stream_interval", 1.0)
    try:
        return max(float(raw_value), 0.1)
    except (TypeError, ValueError):
        return 1.0


def _extract_message_id(response: dict[str, Any]) -> str:
    return str(
        response.get("msgid")
        or response.get("msg_id")
        or response.get("errcode", "")
    )


def _extract_encrypted_value(raw_xml: str) -> str:
    root = _coerce_xml(raw_xml)
    if root is None:
        return ""
    return _xml_text(root, "Encrypt")


def _decrypt_wecom_message(
    *,
    encrypted: str,
    encoding_aes_key: str,
    corp_id: str = "",
) -> str:
    if not encoding_aes_key:
        return encrypted
    aes_key = base64.b64decode(f"{encoding_aes_key}=")
    iv = aes_key[:16]
    cipher_bytes = base64.b64decode(encrypted)
    completed = subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-K",
            aes_key.hex(),
            "-iv",
            iv.hex(),
            "-nopad",
        ],
        input=cipher_bytes,
        capture_output=True,
        check=True,
    )
    plaintext = _pkcs7_unpad(completed.stdout)
    if len(plaintext) < 20:
        raise ValueError("WeCom decrypted payload is too short")
    msg_len = unpack(">I", plaintext[16:20])[0]
    xml_bytes = plaintext[20:20 + msg_len]
    corp_id_bytes = plaintext[20 + msg_len:]
    payload_corp_id = corp_id_bytes.decode("utf-8")
    if corp_id and payload_corp_id and payload_corp_id != corp_id:
        raise ValueError("WeCom corp_id mismatch")
    return xml_bytes.decode("utf-8")


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad <= 0 or pad > 32:
        raise ValueError("Invalid PKCS7 padding")
    return data[:-pad]
