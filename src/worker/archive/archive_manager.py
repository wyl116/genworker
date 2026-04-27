"""
Archive manager - versioning, lifecycle archiving, and log retention.

Provides four archive operations (goal, duty, rule, episode),
file versioning with SHA256 change detection, and execution log archiving.

All archived files are written to archive/{type}/{quarter}/ directories.
Archive directory is append-only - archived files are never modified.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Frontmatter pattern
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArchiveMetadata:
    """Metadata attached to archived items."""
    archived_at: str           # ISO 8601
    archived_by: str           # "system" | "admin:{name}"
    reason: str
    final_summary: str = ""
    outcome: str = ""          # "success" | "partial" | "abandoned" (Goal-specific)
    derived_duties: tuple[str, ...] = ()  # Duties derived from completed Goal


@dataclass(frozen=True)
class ArchivePolicy:
    """Configuration for archive behavior."""
    auto_archive: bool = True
    version_on_change: bool = True
    execution_log_retention: str = "30d"
    archive_retention: str = "365d"
    archive_summary: bool = True


@dataclass(frozen=True)
class VersionRecord:
    """Record of a file version snapshot."""
    original_path: str
    version_path: str
    version_label: str         # e.g. "PERSONA.md.2026-03-15.v1.md"
    created_at: str
    change_summary: str = ""


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def append_archive_metadata(content: str, metadata: ArchiveMetadata) -> str:
    """
    Pure function: append archive metadata block into YAML frontmatter.

    If the content has existing frontmatter, the archive block is merged in.
    If not, a new frontmatter section is created.
    """
    archive_block = {
        "archive": {
            "archived_at": metadata.archived_at,
            "archived_by": metadata.archived_by,
            "reason": metadata.reason,
        }
    }
    if metadata.final_summary:
        archive_block["archive"]["final_summary"] = metadata.final_summary
    if metadata.outcome:
        archive_block["archive"]["outcome"] = metadata.outcome
    if metadata.derived_duties:
        archive_block["archive"]["derived_duties"] = list(metadata.derived_duties)

    match = _FRONTMATTER_RE.match(content)
    if match:
        existing_yaml = yaml.safe_load(match.group(1)) or {}
        merged = {**existing_yaml, **archive_block}
        yaml_str = yaml.dump(
            merged,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        ).rstrip("\n")
        body = match.group(2)
        return f"---\n{yaml_str}\n---\n{body}"

    yaml_str = yaml.dump(
        archive_block,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    ).rstrip("\n")
    return f"---\n{yaml_str}\n---\n{content}"


def _quarter_label(dt: datetime) -> str:
    """Get quarter label like '2026-Q1'."""
    quarter = (dt.month - 1) // 3 + 1
    return f"{dt.year}-Q{quarter}"


def _parse_retention_days(retention: str) -> int:
    """Parse retention string like '30d' to integer days."""
    match = re.match(r"^(\d+)d$", retention.strip())
    if not match:
        return 30  # fallback default
    return int(match.group(1))


def _compute_sha256(file_path: Path) -> str:
    """Compute SHA256 hex digest of a file."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# ArchiveManager
# ---------------------------------------------------------------------------

class ArchiveManager:
    """
    Manages archiving of goals, duties, rules, and episodes.

    Also handles file versioning and execution log archiving.
    """

    def __init__(self, worker_base: Path, policy: ArchivePolicy | None = None):
        self._worker_base = worker_base
        self._policy = policy or ArchivePolicy()
        self._archive_base = worker_base / "archive"

    @property
    def policy(self) -> ArchivePolicy:
        return self._policy

    # -------------------------------------------------------------------
    # Archive operations
    # -------------------------------------------------------------------

    async def archive_goal(
        self,
        goal_path: Path,
        metadata: ArchiveMetadata,
    ) -> Path:
        """Archive a goal file to archive/goals/{quarter}/."""
        return await self._archive_item(goal_path, "goals", metadata)

    async def archive_duty(
        self,
        duty_path: Path,
        metadata: ArchiveMetadata,
    ) -> Path:
        """Archive a duty file to archive/duties/{quarter}/."""
        return await self._archive_item(duty_path, "duties", metadata)

    async def archive_rule(
        self,
        rule_path: Path,
        metadata: ArchiveMetadata,
    ) -> Path:
        """Archive a rule file to archive/rules/{quarter}/."""
        return await self._archive_item(rule_path, "rules", metadata)

    async def archive_episode(
        self,
        episode_path: Path,
        metadata: ArchiveMetadata,
    ) -> Path:
        """Archive an episode file to archive/memory/{quarter}/."""
        return await self._archive_item(episode_path, "memory", metadata)

    async def _archive_item(
        self,
        source_path: Path,
        archive_type: str,
        metadata: ArchiveMetadata,
    ) -> Path:
        """
        Generic archive operation:
        1. Read source file
        2. Append archive metadata to frontmatter
        3. Write to archive/{type}/{quarter}/
        4. Delete original file
        """
        if not source_path.exists():
            raise FileNotFoundError(f"Source file not found: {source_path}")

        content = source_path.read_text(encoding="utf-8")
        archived_content = append_archive_metadata(content, metadata)

        now = datetime.fromisoformat(metadata.archived_at)
        quarter = _quarter_label(now)
        target_dir = self._archive_base / archive_type / quarter
        target_dir.mkdir(parents=True, exist_ok=True)

        target_path = target_dir / source_path.name
        target_path.write_text(archived_content, encoding="utf-8")
        source_path.unlink()

        logger.info(
            "Archived %s -> %s", source_path, target_path,
        )
        return target_path

    # -------------------------------------------------------------------
    # Version management
    # -------------------------------------------------------------------

    def version_file(
        self,
        original_path: Path,
        change_summary: str = "",
    ) -> VersionRecord:
        """
        Create a version snapshot of a file.

        Version naming: {filename}.{date}.v{N}.md
        Version number increments within the same day.
        """
        if not original_path.exists():
            raise FileNotFoundError(f"File not found: {original_path}")

        versions_dir = self._archive_base / "versions"
        versions_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        stem = original_path.name

        # Find next version number for this file on this date
        version_num = self._next_version_number(
            versions_dir, stem, date_str,
        )

        version_label = f"{stem}.{date_str}.v{version_num}.md"
        version_path = versions_dir / version_label

        content = original_path.read_text(encoding="utf-8")
        version_path.write_text(content, encoding="utf-8")

        record = VersionRecord(
            original_path=str(original_path),
            version_path=str(version_path),
            version_label=version_label,
            created_at=now.isoformat(),
            change_summary=change_summary,
        )
        logger.info("Created version: %s", version_label)
        return record

    def _next_version_number(
        self,
        versions_dir: Path,
        stem: str,
        date_str: str,
    ) -> int:
        """Find the next version number for a file on a given date."""
        prefix = f"{stem}.{date_str}.v"
        max_version = 0
        for existing in versions_dir.iterdir():
            name = existing.name
            if name.startswith(prefix) and name.endswith(".md"):
                try:
                    version_part = name[len(prefix):-3]  # strip ".md"
                    num = int(version_part)
                    max_version = max(max_version, num)
                except ValueError:
                    continue
        return max_version + 1

    def detect_change(self, file_path: Path, known_hash: str) -> bool:
        """Detect file change by comparing SHA256 hashes."""
        if not file_path.exists():
            return True
        current_hash = _compute_sha256(file_path)
        return current_hash != known_hash

    @staticmethod
    def compute_hash(file_path: Path) -> str:
        """Compute SHA256 hash of a file (public utility)."""
        return _compute_sha256(file_path)

    # -------------------------------------------------------------------
    # Execution log archiving
    # -------------------------------------------------------------------

    async def archive_stale_logs(self, duties_dir: Path) -> int:
        """
        Archive execution log entries older than retention policy.

        Scans each duty's execution_log.jsonl, moves stale entries to
        archive/execution_logs/{year-month}/{duty_id}-{year-month}.jsonl,
        rewrites the active log with only fresh entries.

        Returns the number of archived entries.
        """
        retention_days = _parse_retention_days(
            self._policy.execution_log_retention,
        )
        cutoff = datetime.now(timezone.utc).timestamp() - (
            retention_days * 86400
        )

        archived_count = 0
        if not duties_dir.is_dir():
            return 0

        for duty_id in _iter_defined_duty_ids(duties_dir):
            log_file = duties_dir / duty_id / "execution_log.jsonl"
            if not log_file.exists():
                continue

            count = self._archive_log_file(log_file, duty_id, cutoff)
            archived_count += count

        return archived_count

    def _archive_log_file(
        self,
        log_file: Path,
        duty_id: str,
        cutoff_timestamp: float,
    ) -> int:
        """Archive stale entries from a single log file."""
        lines = log_file.read_text(encoding="utf-8").strip().split("\n")
        fresh: list[str] = []
        stale_by_month: dict[str, list[str]] = {}

        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                fresh.append(line)
                continue

            ts = entry.get("timestamp", 0)
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts).timestamp()
                except ValueError:
                    fresh.append(line)
                    continue

            if ts < cutoff_timestamp:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                month_key = dt.strftime("%Y-%m")
                stale_by_month.setdefault(month_key, []).append(line)
            else:
                fresh.append(line)

        if not stale_by_month:
            return 0

        # Write stale entries to archive
        total_archived = 0
        for month_key, entries in stale_by_month.items():
            archive_dir = (
                self._archive_base / "execution_logs" / month_key
            )
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_file = archive_dir / f"{duty_id}-{month_key}.jsonl"

            # Append to existing archive file
            with open(archive_file, "a", encoding="utf-8") as f:
                for entry_line in entries:
                    f.write(entry_line + "\n")
            total_archived += len(entries)

        # Rewrite active log with only fresh entries
        log_file.write_text(
            "\n".join(fresh) + ("\n" if fresh else ""),
            encoding="utf-8",
        )

        logger.info(
            "Archived %d stale log entries for duty %s",
            total_archived, duty_id,
        )
        return total_archived


def _iter_defined_duty_ids(duties_dir: Path) -> tuple[str, ...]:
    """Return canonical duty IDs from duty definition files."""
    from src.worker.duty.parser import parse_duty

    duty_ids: list[str] = []
    seen: set[str] = set()
    for duty_file in sorted(duties_dir.glob("*.md")):
        try:
            duty = parse_duty(duty_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "Skipping invalid duty definition %s while archiving logs: %s",
                duty_file,
                exc,
            )
            continue
        if duty.duty_id in seen:
            logger.warning(
                "Skipping duplicate duty_id %s from %s while archiving logs",
                duty.duty_id,
                duty_file,
            )
            continue
        seen.add(duty.duty_id)
        duty_ids.append(duty.duty_id)
    return tuple(duty_ids)
