# edition: baseline
from __future__ import annotations

from pathlib import Path

import pytest

from src.worker.sensing.sensors.workspace_file_sensor import WorkspaceFileSensor


@pytest.mark.asyncio
async def test_workspace_file_sensor_detects_new_and_modified_files(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target = watch_dir / "a.md"
    target.write_text("v1", encoding="utf-8")

    sensor = WorkspaceFileSensor(
        watch_paths=(str(watch_dir),),
        patterns=("*.md",),
    )

    first = await sensor.poll()
    second = await sensor.poll()
    target.write_text("v2", encoding="utf-8")
    third = await sensor.poll()

    assert len(first) == 1
    assert first[0].payload_dict["path"] == str(target)
    assert second == ()
    assert len(third) == 1
    assert third[0].payload_dict["path"] == str(target)


@pytest.mark.asyncio
async def test_workspace_file_sensor_respects_patterns(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    (watch_dir / "a.txt").write_text("txt", encoding="utf-8")
    (watch_dir / "b.md").write_text("md", encoding="utf-8")

    sensor = WorkspaceFileSensor(
        watch_paths=(str(watch_dir),),
        patterns=("*.md",),
    )

    facts = await sensor.poll()

    assert len(facts) == 1
    assert facts[0].payload_dict["path"].endswith("b.md")
