# edition: baseline
from __future__ import annotations

import pytest

from src.common.worker_channel_credentials import (
    EmailCredential,
    FeishuCredential,
    SlackCredential,
    WorkerChannelCredentials,
    parse_worker_channel_credentials,
)


def test_parse_worker_channel_credentials_returns_structured_models() -> None:
    parsed = parse_worker_channel_credentials({
        "feishu": {"app_id": "cli_alice", "app_secret": "secret"},
        "email": {
            "worker_address": "alice@example.com",
            "worker_username": "alice",
            "worker_password": "pw",
            "worker_imap_host": "imap.example.com",
            "worker_smtp_host": "smtp.example.com",
        },
    })

    assert parsed == WorkerChannelCredentials(
        feishu=FeishuCredential(app_id="cli_alice", app_secret="secret"),
        email=EmailCredential(
            worker_address="alice@example.com",
            worker_username="alice",
            worker_password="pw",
            worker_imap_host="imap.example.com",
            worker_smtp_host="smtp.example.com",
        ),
    )


def test_parse_worker_channel_credentials_allows_missing_platforms() -> None:
    parsed = parse_worker_channel_credentials({})

    assert parsed == WorkerChannelCredentials()


def test_parse_worker_channel_credentials_rejects_missing_required_fields() -> None:
    with pytest.raises(ValueError, match="email.worker_username is required"):
        parse_worker_channel_credentials({
            "email": {
                "worker_address": "alice@example.com",
                "worker_password": "pw",
                "worker_imap_host": "imap.example.com",
                "worker_smtp_host": "smtp.example.com",
            },
        })


def test_parse_worker_channel_credentials_supports_slack() -> None:
    parsed = parse_worker_channel_credentials({
        "slack": {
            "bot_token": "xoxb-test",
            "app_token": "xapp-test",
            "signing_secret": "secret",
            "team_id": "T123",
        },
    })

    assert parsed == WorkerChannelCredentials(
        slack=SlackCredential(
            bot_token="xoxb-test",
            app_token="xapp-test",
            signing_secret="secret",
            team_id="T123",
        )
    )


def test_parse_worker_channel_credentials_rejects_invalid_slack_bot_token() -> None:
    with pytest.raises(ValueError, match="slack.bot_token must start with 'xoxb-'"):
        parse_worker_channel_credentials({
            "slack": {"bot_token": "xapp-invalid"},
        })
