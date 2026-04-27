# edition: baseline
"""
Unit tests for MountManager and FileRouter - mounts, caching, credential refresh.
"""
import pytest
import time
from pathlib import Path
from unittest.mock import AsyncMock

from src.worker.data_access.models import DataSpaceConfig, MountConfig
from src.worker.data_access.mount_manager import (
    MountManager,
    FileRouter,
    MountPermissionError,
    MountAuthError,
    MountNotFoundError,
)
from src.worker.data_access.workspace_accessor import WorkspaceAccessor
from src.events.models import Event


def _feishu_mount(
    mount_path: str = "mounts/feishu/",
    permissions: tuple[str, ...] = ("read",),
    cache_ttl: int = 300,
) -> MountConfig:
    return MountConfig(
        mount_id="feishu-1",
        type="feishu",
        source=(("space_id", "sp1"),),
        mount_path=mount_path,
        permissions=permissions,
        cache_ttl=cache_ttl,
    )


def _mock_event_bus() -> AsyncMock:
    bus = AsyncMock()
    bus.publish = AsyncMock()
    return bus


def _mock_platform_client(
    download_data: bytes = b"file-content",
    list_result: list[str] | None = None,
) -> AsyncMock:
    client = AsyncMock()
    client.download = AsyncMock(return_value=download_data)
    client.upload = AsyncMock()
    client.list_files = AsyncMock(return_value=list_result or ["a.txt", "b.txt"])
    client._get_token = AsyncMock(return_value="tok-123")
    return client


def _make_mount_manager(
    mount: MountConfig | None = None,
    event_bus: AsyncMock | None = None,
    platform_client: AsyncMock | None = None,
    tmp_path: Path | None = None,
) -> MountManager:
    m = mount or _feishu_mount()
    eb = event_bus or _mock_event_bus()
    pc = platform_client or _mock_platform_client()
    scratch = tmp_path or Path("/tmp/test_scratch")
    return MountManager(
        mounts=(m,),
        scratch_dir=scratch,
        event_bus=eb,
        platform_clients={m.type: pc},
        tenant_id="demo",
        worker_id="worker-1",
    )


class TestResolveMount:
    """Tests for resolve_mount."""

    def test_resolves_matching_path(self) -> None:
        mm = _make_mount_manager()
        result = mm.resolve_mount("mounts/feishu/doc.md")
        assert result is not None
        assert result.mount_id == "feishu-1"

    def test_returns_none_for_unknown_path(self) -> None:
        mm = _make_mount_manager()
        result = mm.resolve_mount("mounts/unknown/file.txt")
        assert result is None


class TestReadFile:
    """Tests for read_file with caching and credential refresh."""

    @pytest.mark.asyncio
    async def test_read_downloads_file(self) -> None:
        client = _mock_platform_client(download_data=b"hello")
        mm = _make_mount_manager(platform_client=client)
        data = await mm.read_file("mounts/feishu/doc.md")
        assert data == b"hello"
        client.download.assert_called_once()

    @pytest.mark.asyncio
    async def test_read_uses_cache_within_ttl(self) -> None:
        client = _mock_platform_client(download_data=b"hello")
        mm = _make_mount_manager(platform_client=client)
        await mm.read_file("mounts/feishu/doc.md")
        await mm.read_file("mounts/feishu/doc.md")
        assert client.download.call_count == 1

    @pytest.mark.asyncio
    async def test_read_refreshes_on_401(self) -> None:
        client = _mock_platform_client()
        call_count = 0

        async def download_side_effect(source, path, token):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise PermissionError("401 Unauthorized")
            return b"refreshed-content"

        client.download = AsyncMock(side_effect=download_side_effect)
        client._get_token = AsyncMock(side_effect=["tok-123", "new-tok"])
        mm = _make_mount_manager(platform_client=client)
        data = await mm.read_file("mounts/feishu/doc.md")
        assert data == b"refreshed-content"
        assert client._get_token.await_count == 2

    @pytest.mark.asyncio
    async def test_read_raises_on_refresh_failure(self) -> None:
        client = _mock_platform_client()
        client.download = AsyncMock(side_effect=PermissionError("403"))
        client._get_token = AsyncMock(return_value="")
        bus = _mock_event_bus()
        mm = _make_mount_manager(
            platform_client=client, event_bus=bus,
        )
        with pytest.raises(MountAuthError):
            await mm.read_file("mounts/feishu/doc.md")
        event = bus.publish.await_args.args[0]
        assert isinstance(event, Event)
        assert event.type == "mount.auth_expired"
        assert dict(event.payload) == {
            "mount_id": "feishu-1",
            "platform": "feishu",
            "worker_id": "worker-1",
        }

    @pytest.mark.asyncio
    async def test_read_no_read_permission_denied(self) -> None:
        mount = _feishu_mount(permissions=("write",))
        mm = _make_mount_manager(mount=mount)
        with pytest.raises(MountPermissionError):
            await mm.read_file("mounts/feishu/doc.md")

    @pytest.mark.asyncio
    async def test_read_unknown_mount_raises(self) -> None:
        mm = _make_mount_manager()
        with pytest.raises(MountNotFoundError):
            await mm.read_file("mounts/unknown/file.txt")


class TestWriteFile:
    """Tests for write_file."""

    @pytest.mark.asyncio
    async def test_write_uploads_file(self) -> None:
        mount = _feishu_mount(permissions=("read", "write"))
        client = _mock_platform_client()
        mm = _make_mount_manager(mount=mount, platform_client=client)
        await mm.write_file("mounts/feishu/output.md", b"content")
        client.upload.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_no_write_permission_denied(self) -> None:
        mount = _feishu_mount(permissions=("read",))
        mm = _make_mount_manager(mount=mount)
        with pytest.raises(MountPermissionError):
            await mm.write_file("mounts/feishu/output.md", b"content")


class TestListDirectory:
    """Tests for list_directory."""

    @pytest.mark.asyncio
    async def test_list_returns_files(self) -> None:
        client = _mock_platform_client(list_result=["x.txt", "y.pdf"])
        mm = _make_mount_manager(platform_client=client)
        files = await mm.list_directory("mounts/feishu/folder/")
        assert files == ["x.txt", "y.pdf"]


class TestCacheCleanup:
    """Tests for cleanup_cache."""

    @pytest.mark.asyncio
    async def test_cleanup_removes_expired_entries(self) -> None:
        mount = _feishu_mount(cache_ttl=0)
        client = _mock_platform_client(download_data=b"data")
        mm = _make_mount_manager(mount=mount, platform_client=client)
        await mm.read_file("mounts/feishu/doc.md")
        time.sleep(0.01)
        removed = await mm.cleanup_cache()
        assert removed == 1


class TestRefreshCredential:
    """Tests for refresh_credential."""

    @pytest.mark.asyncio
    async def test_refresh_success(self) -> None:
        client = _mock_platform_client()
        client._get_token = AsyncMock(return_value="new-token")
        mm = _make_mount_manager(platform_client=client)
        result = await mm.refresh_credential("feishu-1")
        assert result is True

    @pytest.mark.asyncio
    async def test_refresh_failure(self) -> None:
        client = _mock_platform_client()
        client._get_token = AsyncMock(return_value="")
        mm = _make_mount_manager(platform_client=client)
        result = await mm.refresh_credential("feishu-1")
        assert result is False


class TestFileRouter:
    """Tests for FileRouter - data/ vs mounts/ routing."""

    @pytest.mark.asyncio
    async def test_read_data_prefix_routes_to_workspace(
        self, tmp_path: Path,
    ) -> None:
        data_dir = tmp_path / "data"
        (data_dir / "uploads").mkdir(parents=True)
        (data_dir / "uploads" / "file.txt").write_bytes(b"ws-content")
        ws = WorkspaceAccessor(tmp_path, DataSpaceConfig(root="data/"))
        mm = _make_mount_manager()
        router = FileRouter(ws, mm)
        content = await router.read("data/uploads/file.txt")
        assert content == b"ws-content"

    @pytest.mark.asyncio
    async def test_read_mounts_prefix_routes_to_mount(self) -> None:
        client = _mock_platform_client(download_data=b"mount-content")
        mm = _make_mount_manager(platform_client=client)
        ws = AsyncMock()
        router = FileRouter(ws, mm)
        content = await router.read("mounts/feishu/doc.md")
        assert content == b"mount-content"

    @pytest.mark.asyncio
    async def test_write_data_prefix_routes_to_workspace(
        self, tmp_path: Path,
    ) -> None:
        data_dir = tmp_path / "data"
        (data_dir / "outputs").mkdir(parents=True)
        ws = WorkspaceAccessor(tmp_path, DataSpaceConfig(root="data/"))
        mm = _make_mount_manager()
        router = FileRouter(ws, mm)
        await router.write("data/outputs/result.txt", b"result")
        assert (data_dir / "outputs" / "result.txt").read_bytes() == b"result"

    @pytest.mark.asyncio
    async def test_write_mounts_prefix_routes_to_mount(self) -> None:
        mount = _feishu_mount(permissions=("read", "write"))
        client = _mock_platform_client()
        mm = _make_mount_manager(mount=mount, platform_client=client)
        ws = AsyncMock()
        router = FileRouter(ws, mm)
        await router.write("mounts/feishu/doc.md", b"content")
        client.upload.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_prefix_raises(self) -> None:
        mm = _make_mount_manager()
        ws = AsyncMock()
        router = FileRouter(ws, mm)
        with pytest.raises(ValueError, match="Unknown path prefix"):
            await router.read("unknown/path")
