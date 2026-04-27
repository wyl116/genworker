"""Slack platform service."""

from .auth import SlackAuth
from .client import SlackClient
from .config import SlackConfig
from .exceptions import SlackAPIError

__all__ = [
    "SlackAuth",
    "SlackClient",
    "SlackConfig",
    "SlackAPIError",
]
