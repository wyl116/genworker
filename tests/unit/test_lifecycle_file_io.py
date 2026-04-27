# edition: baseline
from pathlib import Path

from src.worker.lifecycle.file_io import atomic_write_text


def test_atomic_write_text_replaces_content_without_temp_leak(tmp_path: Path):
    path = tmp_path / "worker" / "duties" / "weekly-report.md"

    atomic_write_text(path, "first")
    atomic_write_text(path, "second")

    assert path.read_text(encoding="utf-8") == "second"
    assert list(path.parent.glob(".*.tmp")) == []
