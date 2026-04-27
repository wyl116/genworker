"""Validation helpers for worker IM config APIs."""
from __future__ import annotations

from typing import Any


class IMConfigValidationError(ValueError):
    """Structured validation error for IM config payloads."""

    def __init__(self, details: list[dict[str, Any]]) -> None:
        super().__init__("Invalid IM config payload")
        self.details = details


def validate_im_config_payload(
    *,
    channels: list[dict[str, Any]],
    credentials: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate and normalize supported IM config payloads."""
    details: list[dict[str, Any]] = []
    normalized_channels: list[dict[str, Any]] = []
    normalized_credentials = _normalize_credentials(credentials)
    seen_types: set[str] = set()

    for index, raw_channel in enumerate(channels):
        channel = _normalize_channel(raw_channel)
        channel_type = str(channel.get("type", "")).strip().lower()
        if channel_type not in {"feishu", "slack"}:
            details.append(_detail(
                ["persona", "channels", index, "type"],
                "type must be one of: feishu, slack",
            ))
            continue
        if channel_type in seen_types:
            details.append(_detail(
                ["persona", "channels", index, "type"],
                f"duplicate channel type '{channel_type}'",
            ))
            continue
        seen_types.add(channel_type)

        reply_mode = str(channel.get("reply_mode", "complete")).strip().lower() or "complete"
        if reply_mode not in {"complete", "streaming"}:
            details.append(_detail(
                ["persona", "channels", index, "reply_mode"],
                "reply_mode must be one of: complete, streaming",
            ))

        connection_mode = str(channel.get("connection_mode", "")).strip().lower()
        if channel_type == "feishu" and connection_mode != "websocket":
            details.append(_detail(
                ["persona", "channels", index, "connection_mode"],
                "connection_mode must be websocket",
            ))
        if channel_type == "slack" and connection_mode != "socket_mode":
            details.append(_detail(
                ["persona", "channels", index, "connection_mode"],
                "connection_mode must be socket_mode",
            ))

        chat_ids = _normalize_chat_ids(channel.get("chat_ids"))
        if not chat_ids:
            details.append(_detail(
                ["persona", "channels", index, "chat_ids"],
                "chat_ids must contain at least one value",
            ))

        feature_map = channel.get("features", {})
        if feature_map is None:
            feature_map = {}
        if not isinstance(feature_map, dict):
            details.append(_detail(
                ["persona", "channels", index, "features"],
                "features must be an object",
            ))
            feature_map = {}

        normalized_channels.append({
            "type": channel_type,
            "connection_mode": connection_mode,
            "chat_ids": chat_ids,
            "reply_mode": reply_mode,
            "features": dict(feature_map),
        })

    _validate_required_credentials(
        channels=normalized_channels,
        credentials=normalized_credentials,
        details=details,
    )
    if details:
        raise IMConfigValidationError(details)
    return normalized_channels, normalized_credentials


def mask_credentials(payload: dict[str, Any]) -> dict[str, Any]:
    """Mask secrets before returning config to API callers."""
    result: dict[str, Any] = {}
    for platform, raw in dict(payload or {}).items():
        if not isinstance(raw, dict):
            continue
        masked: dict[str, Any] = {}
        for key, value in raw.items():
            text = str(value or "")
            if _is_secret_field(str(key)):
                masked[key] = _mask_secret(text)
            else:
                masked[key] = text
        result[str(platform)] = masked
    return result


def _normalize_credentials(credentials: dict[str, Any]) -> dict[str, Any]:
    payload = dict(credentials or {})
    result: dict[str, Any] = {}
    for platform in ("feishu", "slack"):
        raw = payload.get(platform)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            result[platform] = raw
            continue
        result[platform] = {
            str(key): str(value or "").strip()
            for key, value in raw.items()
            if str(value or "").strip()
        }
    return result


def _validate_required_credentials(
    *,
    channels: list[dict[str, Any]],
    credentials: dict[str, Any],
    details: list[dict[str, Any]],
) -> None:
    channel_index = {channel["type"]: idx for idx, channel in enumerate(channels)}
    if "feishu" in channel_index:
        feishu = credentials.get("feishu")
        if not isinstance(feishu, dict):
            details.append(_detail(["credentials", "feishu"], "feishu credentials must be an object"))
        else:
            if not feishu.get("app_id"):
                details.append(_detail(["credentials", "feishu", "app_id"], "app_id is required"))
            if not feishu.get("app_secret"):
                details.append(_detail(["credentials", "feishu", "app_secret"], "app_secret is required"))

    if "slack" in channel_index:
        slack = credentials.get("slack")
        if not isinstance(slack, dict):
            details.append(_detail(["credentials", "slack"], "slack credentials must be an object"))
        else:
            bot_token = str(slack.get("bot_token", "")).strip()
            if not bot_token:
                details.append(_detail(["credentials", "slack", "bot_token"], "bot_token is required"))
            elif not bot_token.startswith("xoxb-"):
                details.append(_detail(
                    ["credentials", "slack", "bot_token"],
                    "bot_token must start with xoxb-",
                ))
            if channels[channel_index["slack"]]["connection_mode"] == "socket_mode":
                app_token = str(slack.get("app_token", "")).strip()
                if not app_token:
                    details.append(_detail(
                        ["credentials", "slack", "app_token"],
                        "app_token is required when connection_mode=socket_mode",
                    ))


def _normalize_channel(raw_channel: dict[str, Any]) -> dict[str, Any]:
    return dict(raw_channel or {})


def _normalize_chat_ids(value: Any) -> list[str]:
    items = value if isinstance(value, list) else []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        chat_id = str(item or "").strip()
        if not chat_id or chat_id in seen:
            continue
        seen.add(chat_id)
        result.append(chat_id)
    return result


def _detail(loc: list[Any], msg: str) -> dict[str, Any]:
    return {
        "loc": loc,
        "msg": msg,
        "type": "value_error",
    }


def _is_secret_field(name: str) -> bool:
    lowered = name.strip().lower()
    return any(token in lowered for token in ("token", "secret", "password"))


def _mask_secret(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) <= 4:
        return "****"
    return f"{raw[:4]}****"
