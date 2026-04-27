"""
MountManager - external storage mounting with unified file routing.

Provides:
- MountManager: cache + platform adapters + worker-scoped token refresh
- FileRouter: unified path routing (data/ -> workspace, mounts/ -> mount)
"""
import time
import inspect
from pathlib import Path
from typing import Protocol

from src.events.models import Event
from src.worker.data_access.models import MountConfig


class EventBus(Protocol):
    """Protocol for publishing events (Phase 7 dependency - mock in tests)."""

    async def publish(self, event: Event) -> int:
        """Publish an event."""
        ...


class PlatformClient(Protocol):
    """Protocol for platform-specific file operations."""

    async def download(self, source: dict, path: str, token: str) -> bytes:
        """Download file from platform."""
        ...

    async def upload(
        self, source: dict, path: str, content: bytes, token: str,
    ) -> None:
        """Upload file to platform."""
        ...

    async def list_files(self, source: dict, path: str, token: str) -> list[str]:
        """List files at a path."""
        ...


class MountPermissionError(Exception):
    """Raised when mount operation lacks required permission."""


class MountAuthError(Exception):
    """Raised when mount credential refresh fails."""


class MountNotFoundError(Exception):
    """Raised when no mount matches a virtual path."""


class _CacheEntry:
    """Internal mutable cache entry - not exposed externally."""

    __slots__ = ("data", "timestamp")

    def __init__(self, data: bytes, timestamp: float) -> None:
        self.data = data
        self.timestamp = timestamp


class MountManager:
    """External storage mount manager with worker-scoped token refresh."""

    def __init__(
        self,
        mounts: tuple[MountConfig, ...],
        scratch_dir: Path,
        event_bus: EventBus,
        platform_clients: dict[str, PlatformClient] | None = None,
        *,
        tenant_id: str = "demo",
        worker_id: str = "",
    ) -> None:
        self._mounts = mounts
        self._scratch_dir = scratch_dir
        self._event_bus = event_bus
        self._platform_clients = platform_clients or {}
        self._tenant_id = tenant_id
        self._worker_id = worker_id
        self._cache: dict[str, _CacheEntry] = {}
        self._tokens: dict[str, str] = {}
        self._mount_index: dict[str, MountConfig] = {
            mount.mount_id: mount for mount in mounts
        }

    def resolve_mount(self, virtual_path: str) -> MountConfig | None:
        """Resolve a virtual path to its MountConfig."""
        for mount in self._mounts:
            if virtual_path.startswith(mount.mount_path):
                return mount
        return None

    async def refresh_credential(self, mount_id: str) -> bool:
        """Refresh credential for a mount. Returns True on success."""
        mount = self._mount_index.get(mount_id)
        if mount is None:
            return False
        client = self._get_client(mount)
        new_token = await _fetch_client_token(client)
        if not new_token:
            return False
        self._tokens = {**self._tokens, mount_id: new_token}
        return True

    async def read_file(self, virtual_path: str) -> bytes:
        """Read file: permission check -> cache -> download with auto-refresh."""
        mount = self._resolve_or_raise(virtual_path)
        _check_permission(mount, "read")

        cached = self._get_cached(virtual_path, mount.cache_ttl)
        if cached is not None:
            return cached

        rel_path = virtual_path[len(mount.mount_path):]
        token = await self._get_token(mount)
        client = self._get_client(mount)
        source = dict(mount.source)

        try:
            data = await client.download(source, rel_path, token)
        except PermissionError:
            data = await self._retry_after_refresh(
                mount, lambda t: client.download(source, rel_path, t),
            )

        self._set_cached(virtual_path, data)
        return data

    async def write_file(self, virtual_path: str, content: bytes) -> None:
        """Write file: permission check -> upload with auto-refresh."""
        mount = self._resolve_or_raise(virtual_path)
        _check_permission(mount, "write")

        rel_path = virtual_path[len(mount.mount_path):]
        token = await self._get_token(mount)
        client = self._get_client(mount)
        source = dict(mount.source)

        try:
            await client.upload(source, rel_path, content, token)
        except PermissionError:
            await self._retry_after_refresh(
                mount, lambda t: client.upload(source, rel_path, content, t),
            )

        self._set_cached(virtual_path, content)

    async def list_directory(self, virtual_path: str) -> list[str]:
        """List files in a mounted directory."""
        mount = self._resolve_or_raise(virtual_path)
        _check_permission(mount, "read")

        rel_path = virtual_path[len(mount.mount_path):]
        token = await self._get_token(mount)
        client = self._get_client(mount)
        source = dict(mount.source)

        try:
            return await client.list_files(source, rel_path, token)
        except PermissionError:
            return await self._retry_after_refresh(
                mount, lambda t: client.list_files(source, rel_path, t),
            )

    async def cleanup_cache(self) -> int:
        """Remove cache entries older than their mount's cache_ttl."""
        now = time.time()
        expired_keys: list[str] = []
        for key, entry in self._cache.items():
            mount = self.resolve_mount(key)
            ttl = mount.cache_ttl if mount else 300
            if now - entry.timestamp > ttl:
                expired_keys.append(key)
        for key in expired_keys:
            del self._cache[key]
        return len(expired_keys)

    def _resolve_or_raise(self, virtual_path: str) -> MountConfig:
        mount = self.resolve_mount(virtual_path)
        if mount is None:
            raise MountNotFoundError(f"No mount for path: {virtual_path}")
        return mount

    async def _get_token(self, mount: MountConfig) -> str:
        if mount.mount_id in self._tokens:
            return self._tokens[mount.mount_id]
        token = await _fetch_client_token(self._get_client(mount))
        self._tokens = {**self._tokens, mount.mount_id: token}
        return token

    def _get_client(self, mount: MountConfig) -> PlatformClient:
        client = self._platform_clients.get(mount.type)
        if client is None:
            raise ValueError(f"No platform client for type: {mount.type}")
        return client

    def _get_cached(self, key: str, ttl: int) -> bytes | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if time.time() - entry.timestamp > ttl:
            del self._cache[key]
            return None
        return entry.data

    def _set_cached(self, key: str, data: bytes) -> None:
        self._cache[key] = _CacheEntry(data=data, timestamp=time.time())

    async def _retry_after_refresh(self, mount: MountConfig, operation):
        """Attempt credential refresh and retry. Publish event on failure."""
        refreshed = await self.refresh_credential(mount.mount_id)
        if not refreshed:
            event = Event(
                event_id=f"evt-mount-{mount.mount_id}",
                type="mount.auth_expired",
                source="mount_manager",
                tenant_id=self._tenant_id,
                payload=(
                    ("mount_id", mount.mount_id),
                    ("platform", mount.type),
                    ("worker_id", self._worker_id),
                ),
            )
            await self._event_bus.publish(event)
            raise MountAuthError(
                f"Credential refresh failed for mount: {mount.mount_id}"
            )
        new_token = self._tokens[mount.mount_id]
        return await operation(new_token)


class FileRouter:
    """Unified file routing: data/ -> WorkspaceAccessor, mounts/ -> MountManager."""

    def __init__(self, workspace, mount_manager: MountManager) -> None:
        self._workspace = workspace
        self._mount_manager = mount_manager

    async def read(self, path: str) -> bytes:
        """Route read: data/ prefix -> workspace, mounts/ prefix -> mount."""
        if path.startswith("data/"):
            rel = path[len("data/"):]
            return self._workspace.read_file(rel)
        if path.startswith("mounts/"):
            return await self._mount_manager.read_file(path)
        raise ValueError(f"Unknown path prefix: {path}")

    async def write(self, path: str, content: bytes) -> None:
        """Route write: data/ prefix -> workspace, mounts/ prefix -> mount."""
        if path.startswith("data/"):
            rel = path[len("data/"):]
            self._workspace.write_file(rel, content)
            return
        if path.startswith("mounts/"):
            await self._mount_manager.write_file(path, content)
            return
        raise ValueError(f"Unknown path prefix: {path}")


async def _fetch_client_token(client: PlatformClient) -> str:
    getter = getattr(client, "_get_token", None)
    if getter is None:
        getter = getattr(client, "get_token", None)
    if getter is None:
        return ""
    token = getter()
    if inspect.isawaitable(token):
        token = await token
    return str(token or "")


def _check_permission(mount: MountConfig, required: str) -> None:
    """Raise if mount doesn't have the required permission."""
    if required not in mount.permissions:
        raise MountPermissionError(
            f"Mount '{mount.mount_id}' lacks '{required}' permission"
        )
