"""Background watcher for worker runtime file hot reload."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from src.common.logger import get_logger

logger = get_logger()

ReloadFunc = Callable[..., Awaitable[dict]]


@dataclass
class PersonaReloadWatcher:
    """Poll worker runtime markdown files and hot-reload changed workers."""

    workspace_root: Path
    reload_worker: ReloadFunc
    interval_seconds: float = 2.0
    debounce_seconds: float = 1.0
    _mtime_cache: dict[tuple[str, str, str], int] = field(default_factory=dict)
    _last_reload_at: dict[tuple[str, str], float] = field(default_factory=dict)
    _recent_reloads: list[dict] = field(default_factory=list)
    _reload_count: int = 0
    _last_scan_completed_at: str | None = None
    _last_error: str = ""
    _initialized: bool = False
    _task: asyncio.Task | None = None
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    def start(self) -> None:
        """Start polling in the background if not already running."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop polling and wait for the background task to finish."""
        self._stop_event.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def scan_once(self) -> list[dict]:
        """Scan worker runtime files once and reload changed workers."""
        changed: list[dict] = []
        loop = asyncio.get_running_loop()
        now = loop.time()
        current_keys: set[tuple[str, str, str]] = set()
        changed_files_by_worker: dict[tuple[str, str], list[str]] = {}

        for runtime_path in self._iter_runtime_files():
            tenant_id, worker_id, worker_dir = _resolve_worker_scope(
                self.workspace_root,
                runtime_path,
            )
            relative_path = str(runtime_path.relative_to(worker_dir))
            worker_key = (tenant_id, worker_id)
            file_key = (tenant_id, worker_id, relative_path)
            current_keys.add(file_key)
            try:
                mtime_ns = runtime_path.stat().st_mtime_ns
            except FileNotFoundError:
                continue

            previous = self._mtime_cache.get(file_key)
            self._mtime_cache[file_key] = mtime_ns
            if previous is None:
                if self._initialized:
                    changed_files_by_worker.setdefault(worker_key, []).append(relative_path)
                continue
            if previous == mtime_ns:
                continue
            changed_files_by_worker.setdefault(worker_key, []).append(relative_path)

        stale_keys = set(self._mtime_cache) - current_keys
        for stale_key in stale_keys:
            self._mtime_cache.pop(stale_key, None)
            tenant_id, worker_id, relative_path = stale_key
            self._last_reload_at.pop((tenant_id, worker_id), None)
            if self._initialized:
                changed_files_by_worker.setdefault(
                    (tenant_id, worker_id), []
                ).append(relative_path)

        if not self._initialized:
            self._initialized = True
            self._last_scan_completed_at = _utc_now()
            return changed

        for (tenant_id, worker_id), changed_files in sorted(changed_files_by_worker.items()):
            worker_key = (tenant_id, worker_id)
            if now - self._last_reload_at.get(worker_key, 0.0) < self.debounce_seconds:
                continue

            self._last_reload_at[worker_key] = now
            try:
                result = await self._reload_one_worker(
                    worker_id=worker_id,
                    tenant_id=tenant_id,
                    changed_files=tuple(sorted(set(changed_files))),
                )
                self._reload_count += 1
                self._recent_reloads.append({
                    "tenant_id": tenant_id,
                    "worker_id": worker_id,
                    "changed_files": tuple(sorted(set(changed_files))),
                    "reloaded_at": _utc_now(),
                    **result,
                })
                self._recent_reloads = self._recent_reloads[-20:]
                changed.append(result)
                logger.info(
                    "[PersonaReloadWatcher] Reloaded worker '%s' for tenant '%s'",
                    worker_id,
                    tenant_id,
                )
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning(
                    "[PersonaReloadWatcher] Failed to reload worker '%s' for tenant '%s': %s",
                    worker_id,
                    tenant_id,
                    exc,
                )
        self._last_scan_completed_at = _utc_now()
        return changed

    @property
    def operational_snapshot(self) -> dict:
        """Return a serializable watcher status snapshot."""
        return {
            "configured": True,
            "running": self._task is not None and not self._task.done(),
            "interval_seconds": self.interval_seconds,
            "debounce_seconds": self.debounce_seconds,
            "tracked_workers": len({(tenant_id, worker_id) for tenant_id, worker_id, _ in self._mtime_cache}),
            "tracked_files": len(self._mtime_cache),
            "reload_count": self._reload_count,
            "last_scan_completed_at": self._last_scan_completed_at,
            "last_error": self._last_error,
            "recent_reloads": list(self._recent_reloads),
        }

    async def _run_loop(self) -> None:
        """Polling loop."""
        while not self._stop_event.is_set():
            try:
                await self.scan_once()
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("[PersonaReloadWatcher] Scan loop error: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=max(self.interval_seconds, 0.1),
                )
            except asyncio.TimeoutError:
                continue

    async def _reload_one_worker(
        self,
        *,
        worker_id: str,
        tenant_id: str,
        changed_files: tuple[str, ...],
    ) -> dict:
        """Call reload callback, preferring the extended metadata-aware signature."""
        try:
            return await self.reload_worker(
                worker_id=worker_id,
                tenant_id=tenant_id,
                trigger_source="auto",
                changed_files=changed_files,
            )
        except TypeError:
            return await self.reload_worker(worker_id=worker_id, tenant_id=tenant_id)

    def _iter_runtime_files(self) -> tuple[Path, ...]:
        """Collect watched worker runtime markdown files."""
        files: list[Path] = []
        for pattern in (
            "tenants/*/workers/*/PERSONA.md",
            "tenants/*/workers/*/CHANNEL_CREDENTIALS.json",
            "tenants/*/workers/*/duties/*.md",
            "tenants/*/workers/*/goals/*.md",
            "tenants/*/workers/*/rules/directives/*.md",
            "tenants/*/workers/*/rules/learned/*.md",
            "tenants/*/workers/*/skills/**/SKILL.md",
        ):
            files.extend(self.workspace_root.glob(pattern))
        return tuple(sorted(files))


def _utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _resolve_worker_scope(workspace_root: Path, runtime_path: Path) -> tuple[str, str, Path]:
    """Resolve tenant id, worker id and worker root from a watched file path."""
    relative = runtime_path.relative_to(workspace_root)
    parts = relative.parts
    tenant_id = parts[1]
    worker_id = parts[3]
    worker_dir = workspace_root / "tenants" / tenant_id / "workers" / worker_id
    return tenant_id, worker_id, worker_dir
