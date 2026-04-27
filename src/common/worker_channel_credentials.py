"""Worker-scoped channel credential models and parsing helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FeishuCredential:
    app_id: str
    app_secret: str


@dataclass(frozen=True)
class WeComCredential:
    corpid: str
    corpsecret: str
    agent_id: str = ""


@dataclass(frozen=True)
class DingTalkCredential:
    app_key: str
    app_secret: str
    robot_code: str = ""


@dataclass(frozen=True)
class EmailCredential:
    worker_address: str = ""
    worker_username: str = ""
    worker_password: str = ""
    worker_imap_host: str = ""
    worker_imap_port: int = 993
    worker_smtp_host: str = ""
    worker_smtp_port: int = 465
    owner_address: str = ""
    owner_username: str = ""
    owner_password: str = ""
    owner_imap_host: str = ""
    owner_imap_port: int = 993
    owner_smtp_host: str = ""
    owner_smtp_port: int = 465


@dataclass(frozen=True)
class SlackCredential:
    bot_token: str
    app_token: str = ""
    signing_secret: str = ""
    team_id: str = ""


@dataclass(frozen=True)
class WorkerChannelCredentials:
    feishu: FeishuCredential | None = None
    wecom: WeComCredential | None = None
    dingtalk: DingTalkCredential | None = None
    email: EmailCredential | None = None
    slack: SlackCredential | None = None


def parse_worker_channel_credentials(data: dict[str, Any]) -> WorkerChannelCredentials:
    """Parse validated worker channel credentials from JSON data."""
    payload = dict(data or {})
    return WorkerChannelCredentials(
        feishu=_parse_feishu(payload.get("feishu")),
        wecom=_parse_wecom(payload.get("wecom")),
        dingtalk=_parse_dingtalk(payload.get("dingtalk")),
        email=_parse_email(payload.get("email")),
        slack=_parse_slack(payload.get("slack")),
    )


def _parse_feishu(raw: Any) -> FeishuCredential | None:
    if raw is None:
        return None
    data = _require_mapping("feishu", raw)
    return FeishuCredential(
        app_id=_require_str(data, "feishu", "app_id"),
        app_secret=_require_str(data, "feishu", "app_secret"),
    )


def _parse_wecom(raw: Any) -> WeComCredential | None:
    if raw is None:
        return None
    data = _require_mapping("wecom", raw)
    return WeComCredential(
        corpid=_require_str(data, "wecom", "corpid"),
        corpsecret=_require_str(data, "wecom", "corpsecret"),
        agent_id=_optional_str(data.get("agent_id")),
    )


def _parse_dingtalk(raw: Any) -> DingTalkCredential | None:
    if raw is None:
        return None
    data = _require_mapping("dingtalk", raw)
    return DingTalkCredential(
        app_key=_require_str(data, "dingtalk", "app_key"),
        app_secret=_require_str(data, "dingtalk", "app_secret"),
        robot_code=_optional_str(data.get("robot_code")),
    )


def _parse_email(raw: Any) -> EmailCredential | None:
    if raw is None:
        return None
    data = _require_mapping("email", raw)
    return EmailCredential(
        worker_address=_require_str(data, "email", "worker_address"),
        worker_username=_require_str(data, "email", "worker_username"),
        worker_password=_require_str(data, "email", "worker_password"),
        worker_imap_host=_require_str(data, "email", "worker_imap_host"),
        worker_imap_port=_int_value(data.get("worker_imap_port", 993), "email", "worker_imap_port"),
        worker_smtp_host=_require_str(data, "email", "worker_smtp_host"),
        worker_smtp_port=_int_value(data.get("worker_smtp_port", 465), "email", "worker_smtp_port"),
        owner_address=_optional_str(data.get("owner_address")),
        owner_username=_optional_str(data.get("owner_username")),
        owner_password=_optional_str(data.get("owner_password")),
        owner_imap_host=_optional_str(data.get("owner_imap_host")),
        owner_imap_port=_int_value(data.get("owner_imap_port", 993), "email", "owner_imap_port"),
        owner_smtp_host=_optional_str(data.get("owner_smtp_host")),
        owner_smtp_port=_int_value(data.get("owner_smtp_port", 465), "email", "owner_smtp_port"),
    )


def _parse_slack(raw: Any) -> SlackCredential | None:
    if raw is None:
        return None
    data = _require_mapping("slack", raw)
    bot_token = _require_str(data, "slack", "bot_token")
    if not bot_token.startswith("xoxb-"):
        raise ValueError("slack.bot_token must start with 'xoxb-'")
    return SlackCredential(
        bot_token=bot_token,
        app_token=_optional_str(data.get("app_token")),
        signing_secret=_optional_str(data.get("signing_secret")),
        team_id=_optional_str(data.get("team_id")),
    )


def _require_mapping(platform: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{platform} credentials must be a JSON object")
    return raw


def _require_str(data: dict[str, Any], platform: str, field: str) -> str:
    value = _optional_str(data.get(field))
    if not value:
        raise ValueError(f"{platform}.{field} is required")
    return value


def _optional_str(value: Any) -> str:
    return str(value or "").strip()


def _int_value(value: Any, platform: str, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{platform}.{field} must be an integer") from exc
