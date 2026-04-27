# edition: baseline
"""
Tests for archive manager - archiving, versioning, and log retention.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.worker.archive.archive_manager import (
    ArchiveManager,
    ArchiveMetadata,
    ArchivePolicy,
    VersionRecord,
    append_archive_metadata,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_metadata(
    reason: str = "test archive",
    outcome: str = "",
    derived_duties: tuple[str, ...] = (),
) -> ArchiveMetadata:
    return ArchiveMetadata(
        archived_at="2026-04-01T12:00:00+00:00",
        archived_by="system",
        reason=reason,
        final_summary="Test summary",
        outcome=outcome,
        derived_duties=derived_duties,
    )


def _make_policy(retention: str = "30d") -> ArchivePolicy:
    return ArchivePolicy(execution_log_retention=retention)


def _setup_worker_base(tmp_path: Path) -> Path:
    """Create a worker base directory."""
    worker_base = tmp_path / "worker"
    worker_base.mkdir()
    return worker_base


def _write_markdown_file(path: Path, title: str = "Test") -> None:
    """Write a simple markdown file with frontmatter."""
    content = f"---\ntitle: {title}\nstatus: active\n---\n# {title}\n\nContent here.\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_duty_definition(path: Path, duty_id: str) -> None:
    content = (
        "---\n"
        f"duty_id: {duty_id}\n"
        f"title: {duty_id}\n"
        "status: active\n"
        "triggers:\n"
        "  - id: morning\n"
        "    type: schedule\n"
        "    cron: \"0 9 * * *\"\n"
        "quality_criteria:\n"
        "  - complete\n"
        "---\n"
        "Review the latest status.\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# append_archive_metadata (pure function)
# ---------------------------------------------------------------------------

class TestAppendArchiveMetadata:

    def test_append_to_existing_frontmatter(self):
        content = "---\ntitle: My Goal\nstatus: active\n---\n# Goal content\n"
        metadata = _make_metadata()
        result = append_archive_metadata(content, metadata)

        assert "archive:" in result
        assert "archived_at:" in result
        assert "archived_by: system" in result
        assert "reason: test archive" in result
        assert "title: My Goal" in result  # original preserved

    def test_append_to_plain_content(self):
        content = "# Just a heading\n\nSome content.\n"
        metadata = _make_metadata()
        result = append_archive_metadata(content, metadata)

        assert result.startswith("---\n")
        assert "archive:" in result
        assert "# Just a heading" in result

    def test_append_with_outcome(self):
        metadata = _make_metadata(outcome="success")
        result = append_archive_metadata("---\nk: v\n---\nbody", metadata)
        assert "outcome: success" in result

    def test_append_with_derived_duties(self):
        metadata = _make_metadata(derived_duties=("duty-001", "duty-002"))
        result = append_archive_metadata("---\nk: v\n---\nbody", metadata)
        assert "derived_duties:" in result


# ---------------------------------------------------------------------------
# Archive operations
# ---------------------------------------------------------------------------

class TestArchiveGoal:

    @pytest.mark.asyncio
    async def test_archive_goal(self, tmp_path):
        worker_base = _setup_worker_base(tmp_path)
        goal_path = worker_base / "goals" / "goal-001.md"
        _write_markdown_file(goal_path, "My Goal")

        manager = ArchiveManager(worker_base)
        metadata = _make_metadata(outcome="success")

        result = await manager.archive_goal(goal_path, metadata)

        assert result.exists()
        assert "archive" in str(result)
        assert "goals" in str(result)
        assert "2026-Q2" in str(result)  # April is Q2
        assert not goal_path.exists()  # original deleted

        # Verify archive content has metadata
        archived_content = result.read_text()
        assert "archive:" in archived_content

    @pytest.mark.asyncio
    async def test_archive_nonexistent_raises(self, tmp_path):
        worker_base = _setup_worker_base(tmp_path)
        manager = ArchiveManager(worker_base)
        metadata = _make_metadata()

        with pytest.raises(FileNotFoundError):
            await manager.archive_goal(
                worker_base / "goals" / "nonexistent.md",
                metadata,
            )


class TestArchiveDuty:

    @pytest.mark.asyncio
    async def test_archive_duty(self, tmp_path):
        worker_base = _setup_worker_base(tmp_path)
        duty_path = worker_base / "duties" / "duty-001.md"
        _write_markdown_file(duty_path, "My Duty")

        manager = ArchiveManager(worker_base)
        metadata = _make_metadata()

        result = await manager.archive_duty(duty_path, metadata)

        assert result.exists()
        assert "duties" in str(result)
        assert not duty_path.exists()


class TestArchiveRule:

    @pytest.mark.asyncio
    async def test_archive_rule(self, tmp_path):
        worker_base = _setup_worker_base(tmp_path)
        rule_path = worker_base / "rules" / "learned" / "rule-001.md"
        _write_markdown_file(rule_path, "My Rule")

        manager = ArchiveManager(worker_base)
        metadata = _make_metadata()

        result = await manager.archive_rule(rule_path, metadata)

        assert result.exists()
        assert "rules" in str(result)
        assert not rule_path.exists()


class TestArchiveEpisode:

    @pytest.mark.asyncio
    async def test_archive_episode(self, tmp_path):
        worker_base = _setup_worker_base(tmp_path)
        episode_path = worker_base / "memory" / "episodes" / "ep-001.md"
        _write_markdown_file(episode_path, "Episode")

        manager = ArchiveManager(worker_base)
        metadata = _make_metadata()

        result = await manager.archive_episode(episode_path, metadata)

        assert result.exists()
        assert "memory" in str(result)
        assert not episode_path.exists()


# ---------------------------------------------------------------------------
# Version management
# ---------------------------------------------------------------------------

class TestVersionFile:

    def test_create_version(self, tmp_path):
        worker_base = _setup_worker_base(tmp_path)
        original = worker_base / "PERSONA.md"
        original.write_text("# Agent Config v1\n", encoding="utf-8")

        manager = ArchiveManager(worker_base)
        record = manager.version_file(original, "Initial version")

        assert isinstance(record, VersionRecord)
        assert Path(record.version_path).exists()
        assert ".v1.md" in record.version_label
        assert record.change_summary == "Initial version"

    def test_version_increments_same_day(self, tmp_path):
        worker_base = _setup_worker_base(tmp_path)
        original = worker_base / "PERSONA.md"
        original.write_text("# v1\n", encoding="utf-8")

        manager = ArchiveManager(worker_base)

        r1 = manager.version_file(original, "Change 1")
        assert ".v1.md" in r1.version_label

        original.write_text("# v2\n", encoding="utf-8")
        r2 = manager.version_file(original, "Change 2")
        assert ".v2.md" in r2.version_label

        original.write_text("# v3\n", encoding="utf-8")
        r3 = manager.version_file(original, "Change 3")
        assert ".v3.md" in r3.version_label

    def test_version_nonexistent_raises(self, tmp_path):
        worker_base = _setup_worker_base(tmp_path)
        manager = ArchiveManager(worker_base)
        with pytest.raises(FileNotFoundError):
            manager.version_file(worker_base / "missing.md")


class TestDetectChange:

    def test_detect_no_change(self, tmp_path):
        worker_base = _setup_worker_base(tmp_path)
        file_path = worker_base / "test.md"
        file_path.write_text("hello", encoding="utf-8")

        manager = ArchiveManager(worker_base)
        known_hash = manager.compute_hash(file_path)
        assert manager.detect_change(file_path, known_hash) is False

    def test_detect_change(self, tmp_path):
        worker_base = _setup_worker_base(tmp_path)
        file_path = worker_base / "test.md"
        file_path.write_text("hello", encoding="utf-8")

        manager = ArchiveManager(worker_base)
        known_hash = manager.compute_hash(file_path)

        file_path.write_text("changed!", encoding="utf-8")
        assert manager.detect_change(file_path, known_hash) is True

    def test_detect_missing_file(self, tmp_path):
        worker_base = _setup_worker_base(tmp_path)
        manager = ArchiveManager(worker_base)
        assert manager.detect_change(
            worker_base / "missing.md", "abc",
        ) is True


# ---------------------------------------------------------------------------
# Execution log archiving
# ---------------------------------------------------------------------------

class TestArchiveStaleLogs:

    def _make_log_entry(self, ts: float, msg: str = "test") -> str:
        return json.dumps({"timestamp": ts, "message": msg})

    @pytest.mark.asyncio
    async def test_archive_stale_entries(self, tmp_path):
        worker_base = _setup_worker_base(tmp_path)
        duties_dir = worker_base / "duties"
        _write_duty_definition(duties_dir / "duty-001.md", "duty-001")
        duty_dir = duties_dir / "duty-001"
        duty_dir.mkdir(parents=True)

        now = datetime.now(timezone.utc).timestamp()
        old_ts = now - (40 * 86400)  # 40 days ago
        fresh_ts = now - (5 * 86400)  # 5 days ago

        log_file = duty_dir / "execution_log.jsonl"
        lines = [
            self._make_log_entry(old_ts, "old entry"),
            self._make_log_entry(fresh_ts, "fresh entry"),
        ]
        log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        manager = ArchiveManager(worker_base, _make_policy("30d"))
        count = await manager.archive_stale_logs(duties_dir)

        assert count == 1  # one old entry archived

        # Fresh entry should remain in active log
        remaining = log_file.read_text().strip().split("\n")
        assert len(remaining) == 1
        entry = json.loads(remaining[0])
        assert entry["message"] == "fresh entry"

        # Archive directory should have the old entry
        archive_logs = list(
            (worker_base / "archive" / "execution_logs").rglob("*.jsonl"),
        )
        assert len(archive_logs) == 1

    @pytest.mark.asyncio
    async def test_no_stale_entries(self, tmp_path):
        worker_base = _setup_worker_base(tmp_path)
        duties_dir = worker_base / "duties"
        _write_duty_definition(duties_dir / "duty-001.md", "duty-001")
        duty_dir = duties_dir / "duty-001"
        duty_dir.mkdir(parents=True)

        now = datetime.now(timezone.utc).timestamp()
        fresh_ts = now - (5 * 86400)

        log_file = duty_dir / "execution_log.jsonl"
        log_file.write_text(
            self._make_log_entry(fresh_ts, "fresh") + "\n",
            encoding="utf-8",
        )

        manager = ArchiveManager(worker_base, _make_policy("30d"))
        count = await manager.archive_stale_logs(duties_dir)
        assert count == 0

    @pytest.mark.asyncio
    async def test_empty_duties_dir(self, tmp_path):
        worker_base = _setup_worker_base(tmp_path)
        manager = ArchiveManager(worker_base)
        count = await manager.archive_stale_logs(worker_base / "nonexistent")
        assert count == 0

    @pytest.mark.asyncio
    async def test_multiple_duties(self, tmp_path):
        worker_base = _setup_worker_base(tmp_path)
        duties_dir = worker_base / "duties"

        now = datetime.now(timezone.utc).timestamp()
        old_ts = now - (40 * 86400)

        for duty_id in ("duty-001", "duty-002"):
            _write_duty_definition(duties_dir / f"{duty_id}.md", duty_id)
            duty_dir = duties_dir / duty_id
            duty_dir.mkdir(parents=True)
            log_file = duty_dir / "execution_log.jsonl"
            log_file.write_text(
                self._make_log_entry(old_ts, f"old-{duty_id}") + "\n",
                encoding="utf-8",
            )

        manager = ArchiveManager(worker_base, _make_policy("30d"))
        count = await manager.archive_stale_logs(duties_dir)
        assert count == 2

    @pytest.mark.asyncio
    async def test_orphan_log_directory_is_ignored(self, tmp_path):
        worker_base = _setup_worker_base(tmp_path)
        duties_dir = worker_base / "duties"
        _write_duty_definition(duties_dir / "duty-001.md", "duty-001")

        now = datetime.now(timezone.utc).timestamp()
        old_ts = now - (40 * 86400)

        defined_dir = duties_dir / "duty-001"
        defined_dir.mkdir(parents=True)
        (defined_dir / "execution_log.jsonl").write_text(
            self._make_log_entry(old_ts, "defined") + "\n",
            encoding="utf-8",
        )

        orphan_dir = duties_dir / "orphan-duty"
        orphan_dir.mkdir(parents=True)
        orphan_log = orphan_dir / "execution_log.jsonl"
        orphan_log.write_text(
            self._make_log_entry(old_ts, "orphan") + "\n",
            encoding="utf-8",
        )

        manager = ArchiveManager(worker_base, _make_policy("30d"))
        count = await manager.archive_stale_logs(duties_dir)

        assert count == 1
        assert orphan_log.read_text(encoding="utf-8").strip()
