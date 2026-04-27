"""Feishu service configuration."""
from dataclasses import dataclass


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str = ""
    app_secret: str = ""
    base_url: str = "https://open.feishu.cn/open-apis"

