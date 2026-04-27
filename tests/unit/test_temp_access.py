# edition: baseline
"""
Unit tests for TempAccessManager - strict/permissive modes and expiry.
"""
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.worker.data_access.models import ExternalAccessConfig, TempAccessRecord
from src.worker.data_access.temp_access import (
    TempAccessManager,
    TempAccessDeniedError,
    TempAccessExpiredError,
)


@pytest.fixture
def scratch_dir(tmp_path: Path) -> Path:
    d = tmp_path / "scratch"
    d.mkdir()
    return d


def _strict_config() -> ExternalAccessConfig:
    return ExternalAccessConfig(mode="strict", auto_expire="24h")


def _permissive_config(
    domains: tuple[str, ...] = ("example.com",),
) -> ExternalAccessConfig:
    return ExternalAccessConfig(
        mode="permissive", allowed_domains=domains, auto_expire="24h",
    )


class TestStrictMode:
    """Tests for strict mode - worker self-authorization denied."""

    def test_worker_self_grant_denied(self, scratch_dir: Path) -> None:
        manager = TempAccessManager(scratch_dir, _strict_config())
        with pytest.raises(TempAccessDeniedError, match="strict mode"):
            manager.grant_access(
                "https://example.com/file.pdf",
                granted_by="worker-1",
                worker_id="worker-1",
            )

    def test_user_grant_allowed(self, scratch_dir: Path) -> None:
        manager = TempAccessManager(scratch_dir, _strict_config())
        record = manager.grant_access(
            "https://example.com/file.pdf",
            granted_by="user-alice",
            worker_id="worker-1",
        )
        assert isinstance(record, TempAccessRecord)
        assert record.source_url == "https://example.com/file.pdf"
        assert record.granted_by == "user-alice"
        assert record.granted_to == "worker-1"


class TestPermissiveMode:
    """Tests for permissive mode - whitelist domain check."""

    def test_whitelisted_domain_self_grant_allowed(
        self, scratch_dir: Path,
    ) -> None:
        manager = TempAccessManager(
            scratch_dir, _permissive_config(("example.com",)),
        )
        record = manager.grant_access(
            "https://example.com/doc.pdf",
            granted_by="worker-1",
            worker_id="worker-1",
        )
        assert record.source_url == "https://example.com/doc.pdf"

    def test_subdomain_matches(self, scratch_dir: Path) -> None:
        manager = TempAccessManager(
            scratch_dir, _permissive_config(("example.com",)),
        )
        record = manager.grant_access(
            "https://api.example.com/data",
            granted_by="worker-1",
            worker_id="worker-1",
        )
        assert record is not None

    def test_non_whitelisted_domain_denied(self, scratch_dir: Path) -> None:
        manager = TempAccessManager(
            scratch_dir, _permissive_config(("example.com",)),
        )
        with pytest.raises(TempAccessDeniedError, match="not in allowed"):
            manager.grant_access(
                "https://evil.com/malware",
                granted_by="worker-1",
                worker_id="worker-1",
            )

    def test_user_grant_always_allowed_regardless_of_domain(
        self, scratch_dir: Path,
    ) -> None:
        manager = TempAccessManager(
            scratch_dir, _permissive_config(("example.com",)),
        )
        record = manager.grant_access(
            "https://any-domain.org/file",
            granted_by="user-bob",
            worker_id="worker-1",
        )
        assert record is not None


class TestFetchAndExpiry:
    """Tests for fetch_to_local and cleanup_expired."""

    @pytest.mark.asyncio
    async def test_fetch_creates_local_file(self, scratch_dir: Path) -> None:
        manager = TempAccessManager(scratch_dir, _strict_config())
        record = manager.grant_access(
            "https://example.com/file.pdf",
            granted_by="user-alice",
            worker_id="worker-1",
        )
        local_path = await manager.fetch_to_local(record.access_id)
        assert local_path.exists()

    @pytest.mark.asyncio
    async def test_fetch_expired_raises(self, scratch_dir: Path) -> None:
        manager = TempAccessManager(scratch_dir, _strict_config())
        record = manager.grant_access(
            "https://example.com/file.pdf",
            granted_by="user-alice",
            worker_id="worker-1",
            expires_in="0s",
        )
        # Wait briefly to ensure expiry (0 seconds means immediate)
        import asyncio
        await asyncio.sleep(0.05)
        with pytest.raises(TempAccessExpiredError):
            await manager.fetch_to_local(record.access_id)

    @pytest.mark.asyncio
    async def test_cleanup_removes_expired(self, scratch_dir: Path) -> None:
        manager = TempAccessManager(scratch_dir, _strict_config())
        record = manager.grant_access(
            "https://example.com/file.pdf",
            granted_by="user-alice",
            worker_id="worker-1",
            expires_in="1s",
        )
        await manager.fetch_to_local(record.access_id)
        import asyncio
        await asyncio.sleep(1.1)
        removed = await manager.cleanup_expired()
        assert removed == 1
        assert len(manager.records) == 0

    @pytest.mark.asyncio
    async def test_fetch_unknown_id_raises(self, scratch_dir: Path) -> None:
        manager = TempAccessManager(scratch_dir, _strict_config())
        with pytest.raises(KeyError):
            await manager.fetch_to_local("nonexistent")

    def test_custom_expiry(self, scratch_dir: Path) -> None:
        manager = TempAccessManager(scratch_dir, _strict_config())
        record = manager.grant_access(
            "https://example.com/file",
            granted_by="user-alice",
            worker_id="worker-1",
            expires_in="1h",
        )
        expires = datetime.fromisoformat(record.expires_at)
        now = datetime.now(timezone.utc)
        assert expires > now + timedelta(minutes=50)
