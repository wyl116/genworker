"""Slack service configuration."""
from dataclasses import dataclass


@dataclass(frozen=True)
class SlackConfig:
    bot_token: str = ""
    app_token: str = ""
    signing_secret: str = ""
    team_id: str = ""
    base_url: str = "https://slack.com/api"
