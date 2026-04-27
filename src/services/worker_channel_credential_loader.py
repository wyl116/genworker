"""Load and cache worker-scoped channel credentials from workspace files."""
from __future__ import annotations

import json
from pathlib import Path

from src.common.worker_channel_credentials import (
    WorkerChannelCredentials,
    parse_worker_channel_credentials,
)

_CREDENTIALS_FILENAME = "CHANNEL_CREDENTIALS.json"


class WorkerChannelCredentialLoader:
    """Filesystem-backed loader for per-worker channel credentials."""

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root
        self._cache: dict[tuple[str, str], WorkerChannelCredentials] = {}

    def load(self, tenant_id: str, worker_id: str) -> WorkerChannelCredentials:
        cache_key = (tenant_id, worker_id)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        credentials_path = self._credentials_path(tenant_id, worker_id)
        if not credentials_path.is_file():
            empty = WorkerChannelCredentials()
            self._cache[cache_key] = empty
            return empty

        try:
            raw = json.loads(credentials_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSON in {credentials_path}"
            ) from exc
        if not isinstance(raw, dict):
            raise ValueError(f"{credentials_path} must contain a JSON object")

        parsed = parse_worker_channel_credentials(raw)
        self._cache[cache_key] = parsed
        return parsed

    def clear_cache(
        self,
        tenant_id: str | None = None,
        worker_id: str | None = None,
    ) -> None:
        if tenant_id is None and worker_id is None:
            self._cache.clear()
            return
        for key in tuple(self._cache):
            if tenant_id is not None and key[0] != tenant_id:
                continue
            if worker_id is not None and key[1] != worker_id:
                continue
            self._cache.pop(key, None)

    def _credentials_path(self, tenant_id: str, worker_id: str) -> Path:
        return (
            self._workspace_root
            / "tenants"
            / tenant_id
            / "workers"
            / worker_id
            / _CREDENTIALS_FILENAME
        )
