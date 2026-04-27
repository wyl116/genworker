"""DingTalk platform service."""

from .auth import DingTalkAuth
from .client import DingTalkClient
from .config import DingTalkConfig
from .exceptions import DingTalkAPIError

__all__ = ["DingTalkAuth", "DingTalkClient", "DingTalkConfig", "DingTalkAPIError"]
