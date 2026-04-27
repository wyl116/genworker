# edition: baseline
from __future__ import annotations

from pathlib import Path

from src.services.email import EmailClient
from src.services.feishu import FeishuClient
from src.services.worker_channel_credential_loader import (
    WorkerChannelCredentialLoader,
)
from src.services.worker_platform_client_factory import WorkerPlatformClientFactory


def test_factory_builds_isolated_clients_per_worker(tmp_path: Path) -> None:
    _write_credentials(
        tmp_path,
        tenant_id="demo",
        worker_id="alice",
        payload=(
            '{"email":{"worker_address":"alice@example.com","worker_username":"alice",'
            '"worker_password":"pw","worker_imap_host":"imap.example.com",'
            '"worker_smtp_host":"smtp.example.com"}}'
        ),
    )
    _write_credentials(
        tmp_path,
        tenant_id="demo",
        worker_id="bob",
        payload=(
            '{"email":{"worker_address":"bob@example.com","worker_username":"bob",'
            '"worker_password":"pw","worker_imap_host":"imap.example.com",'
            '"worker_smtp_host":"smtp.example.com"}}'
        ),
    )
    factory = WorkerPlatformClientFactory(WorkerChannelCredentialLoader(tmp_path))

    alice_client = factory.get_client("demo", "alice", "email")
    bob_client = factory.get_client("demo", "bob", "email")

    assert isinstance(alice_client, EmailClient)
    assert isinstance(bob_client, EmailClient)
    assert alice_client is not bob_client


def test_factory_supports_multiple_platforms_for_one_worker(tmp_path: Path) -> None:
    _write_credentials(
        tmp_path,
        tenant_id="demo",
        worker_id="alice",
        payload=(
            '{"feishu":{"app_id":"cli_alice","app_secret":"secret"},'
            '"email":{"worker_address":"alice@example.com","worker_username":"alice",'
            '"worker_password":"pw","worker_imap_host":"imap.example.com",'
            '"worker_smtp_host":"smtp.example.com"}}'
        ),
    )
    factory = WorkerPlatformClientFactory(WorkerChannelCredentialLoader(tmp_path))

    feishu_client = factory.get_client("demo", "alice", "feishu")
    email_client = factory.get_client("demo", "alice", "email")

    assert isinstance(feishu_client, FeishuClient)
    assert isinstance(email_client, EmailClient)


def test_factory_returns_none_when_platform_not_configured(tmp_path: Path) -> None:
    factory = WorkerPlatformClientFactory(WorkerChannelCredentialLoader(tmp_path))

    assert factory.get_client("demo", "alice", "feishu") is None


def _write_credentials(
    root: Path,
    *,
    tenant_id: str,
    worker_id: str,
    payload: str,
) -> None:
    path = (
        root
        / "tenants"
        / tenant_id
        / "workers"
        / worker_id
        / "CHANNEL_CREDENTIALS.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
