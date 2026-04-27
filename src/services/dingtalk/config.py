"""DingTalk service configuration."""
from dataclasses import dataclass


@dataclass(frozen=True)
class DingTalkConfig:
    app_key: str = ""
    app_secret: str = ""
    robot_code: str = ""
    base_url: str = "https://api.dingtalk.com"
    legacy_base_url: str = "https://oapi.dingtalk.com"

