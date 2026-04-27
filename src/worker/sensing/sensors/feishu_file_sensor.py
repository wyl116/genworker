"""Feishu folder polling sensor."""
from __future__ import annotations

from typing import Any

from ..base import SensorBase
from ..protocol import SensedFact


class FeishuFileSensor(SensorBase):
    """Poll Feishu folder metadata and emit file-change facts."""

    def __init__(
        self,
        *,
        feishu_client: Any | None = None,
        mount_manager: Any | None = None,
        filter_config: dict[str, str] | None = None,
    ) -> None:
        super().__init__(fallback_route="heartbeat")
        self._feishu_client = feishu_client
        self._mount_manager = mount_manager
        self._filter = filter_config or {}
        self._snapshot: dict[str, str] = {}

    @property
    def sensor_type(self) -> str:
        return "feishu_folder"

    @property
    def delivery_mode(self) -> str:
        return "poll"

    async def poll(self) -> tuple[SensedFact, ...]:
        current, changed_files = await self._list_changed_files()
        self._snapshot = current
        return tuple(
            SensedFact(
                source_type="feishu_folder",
                event_type="external.feishu_doc_updated",
                dedupe_key=f"feishu:{item['path']}:{item['modified_at']}",
                payload=tuple((str(key), value) for key, value in item.items()),
                priority_hint=15,
                cognition_route="heartbeat",
            )
            for item in changed_files
        )

    async def _list_changed_files(self) -> tuple[dict[str, str], list[dict[str, Any]]]:
        folder_path = self._filter.get("folder_path", "/")
        current_snapshot: dict[str, str] = {}
        changed_files: list[dict[str, Any]] = []

        if self._feishu_client is not None:
            source = {"folder_token": folder_path.strip("/")}
            try:
                files = await self._feishu_client.list_with_metadata(source, folder_path, token="")
            except Exception:
                return ({}, [])
            current_snapshot = {item.name: item.modified_at for item in files}
            for item in files:
                if item.name not in self._snapshot or self._snapshot[item.name] != item.modified_at:
                    changed_files.append(
                        {
                            "name": item.name,
                            "modified_at": item.modified_at,
                            "path": item.path,
                        }
                    )
            return (current_snapshot, changed_files)

        if self._mount_manager is None:
            return ({}, [])

        try:
            files = await self._mount_manager.list_directory(folder_path)
        except Exception:
            return ({}, [])

        current_snapshot = {
            getattr(item, "name", str(item)): getattr(item, "modified_at", "")
            for item in files
        }
        for name, modified_at in current_snapshot.items():
            if name not in self._snapshot or self._snapshot[name] != modified_at:
                changed_files.append(
                    {
                        "name": name,
                        "modified_at": modified_at,
                        "path": f"{folder_path}/{name}",
                    }
                )
        return (current_snapshot, changed_files)

    def get_snapshot(self) -> dict[str, Any]:
        return {"file_snapshot": self._snapshot}

    def restore_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = dict(snapshot.get("file_snapshot", {}))
