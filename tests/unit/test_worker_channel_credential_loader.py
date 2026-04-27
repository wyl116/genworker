# edition: baseline
from __future__ import annotations

from pathlib import Path

import pytest

from src.services.worker_channel_credential_loader import (
    WorkerChannelCredentialLoader,
)


def test_loader_returns_empty_credentials_when_file_missing(tmp_path: Path) -> None:
    loader = WorkerChannelCredentialLoader(tmp_path)

    credentials = loader.load("demo", "alice")

    assert credentials.feishu is None
    assert credentials.email is None


def test_loader_reads_and_caches_worker_credentials(tmp_path: Path) -> None:
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
            '{"feishu":{"app_id":"cli_alice","app_secret":"secret"}}'
        ),
        encoding="utf-8",
    )
    loader = WorkerChannelCredentialLoader(tmp_path)

    first = loader.load("demo", "alice")
    path.write_text(
        '{"feishu":{"app_id":"changed","app_secret":"secret"}}',
        encoding="utf-8",
    )
    second = loader.load("demo", "alice")

    assert first.feishu is not None
    assert first.feishu.app_id == "cli_alice"
    assert second.feishu is not None
    assert second.feishu.app_id == "cli_alice"

    loader.clear_cache(tenant_id="demo", worker_id="alice")
    refreshed = loader.load("demo", "alice")
    assert refreshed.feishu is not None
    assert refreshed.feishu.app_id == "changed"


def test_loader_rejects_invalid_json(tmp_path: Path) -> None:
    path = (
        tmp_path
        / "tenants"
        / "demo"
        / "workers"
        / "alice"
        / "CHANNEL_CREDENTIALS.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{bad json", encoding="utf-8")
    loader = WorkerChannelCredentialLoader(tmp_path)

    with pytest.raises(ValueError, match="Invalid JSON"):
        loader.load("demo", "alice")
