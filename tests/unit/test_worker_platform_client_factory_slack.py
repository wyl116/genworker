# edition: baseline
from __future__ import annotations

from pathlib import Path

from src.services.slack import SlackClient
from src.services.worker_channel_credential_loader import WorkerChannelCredentialLoader
from src.services.worker_platform_client_factory import WorkerPlatformClientFactory


def test_worker_platform_client_factory_builds_slack_client(tmp_path: Path) -> None:
    path = (
        tmp_path
        / "tenants"
        / "demo"
        / "workers"
        / "alice"
        / "CHANNEL_CREDENTIALS.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            '{"slack":{"bot_token":"xoxb-token","app_token":"xapp-token",'
            '"signing_secret":"sig","team_id":"T1"}}'
        ),
        encoding="utf-8",
    )
    loader = WorkerChannelCredentialLoader(tmp_path)
    factory = WorkerPlatformClientFactory(loader)

    client = factory.get_client("demo", "alice", "slack")

    assert isinstance(client, SlackClient)
    assert client is not None
    assert client._config.bot_token == "xoxb-token"
    assert client._config.app_token == "xapp-token"


def test_worker_platform_client_factory_builds_slack_client_without_sdk(tmp_path: Path) -> None:
    path = (
        tmp_path
        / "tenants"
        / "demo"
        / "workers"
        / "alice"
        / "CHANNEL_CREDENTIALS.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"slack":{"bot_token":"xoxb-token"}}', encoding="utf-8")
    loader = WorkerChannelCredentialLoader(tmp_path)
    factory = WorkerPlatformClientFactory(loader)

    client = factory.get_client("demo", "alice", "slack")

    assert isinstance(client, SlackClient)
    assert client._config.bot_token == "xoxb-token"


def test_worker_platform_client_factory_returns_none_without_slack_credentials(tmp_path: Path) -> None:
    factory = WorkerPlatformClientFactory(WorkerChannelCredentialLoader(tmp_path))

    client = factory.get_client("demo", "alice", "slack")

    assert client is None
