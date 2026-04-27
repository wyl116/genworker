# edition: baseline
from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.worker.sensing.sensors.feishu_file_sensor import FeishuFileSensor


@dataclass(frozen=True)
class _FileInfo:
    name: str
    modified_at: str


class _MountManager:
    def __init__(self, files: tuple[_FileInfo, ...]) -> None:
        self.files = files

    async def list_directory(self, virtual_path: str):
        return self.files


@pytest.mark.asyncio
async def test_feishu_file_sensor_detects_changes_from_mount_manager() -> None:
    manager = _MountManager((_FileInfo("doc.md", "2026-04-01T10:00:00"),))
    sensor = FeishuFileSensor(
        mount_manager=manager,
        filter_config={"folder_path": "/docs"},
    )

    first = await sensor.poll()
    second = await sensor.poll()
    manager.files = (_FileInfo("doc.md", "2026-04-02T10:00:00"),)
    third = await sensor.poll()

    assert len(first) == 1
    assert second == ()
    assert len(third) == 1
    assert third[0].payload_dict["path"] == "/docs/doc.md"
