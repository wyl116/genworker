"""WeCom service configuration."""
from dataclasses import dataclass


@dataclass(frozen=True)
class WeComConfig:
    corpid: str = ""
    corpsecret: str = ""
    agent_id: str = ""
    base_url: str = "https://qyapi.weixin.qq.com/cgi-bin"

