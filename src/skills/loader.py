"""
Skill directory scanner - recursively discovers and loads SKILL.md files.

Error tolerant: a single SKILL.md parse failure is logged and skipped
without affecting other skills.
"""
from pathlib import Path
from typing import Sequence

from src.common.logger import get_logger

from .models import Skill
from .parser import SkillParser

logger = get_logger()

_SKILL_FILENAME = "SKILL.md"


class SkillLoader:
    """
    Recursive directory scanner for SKILL.md files.

    Usage:
        loader = SkillLoader()
        skills = loader.scan(Path("workspace/system/skills"))
    """

    def __init__(self, parser: SkillParser | None = None) -> None:
        self._parser = parser or SkillParser()

    def scan(self, directory: Path) -> tuple[Skill, ...]:
        """
        Recursively scan a directory for SKILL.md files.

        Args:
            directory: Root directory to scan.

        Returns:
            Tuple of successfully parsed Skill objects.
            Parse failures are logged and skipped.
        """
        if not directory.is_dir():
            logger.warning(f"[SkillLoader] Directory does not exist: {directory}")
            return ()

        skill_files = _find_skill_files(directory)
        logger.info(
            f"[SkillLoader] Found {len(skill_files)} SKILL.md file(s) in {directory}"
        )

        return _load_all(skill_files, self._parser)

    def scan_multiple(self, directories: Sequence[Path]) -> tuple[Skill, ...]:
        """
        Scan multiple directories and combine results.

        Args:
            directories: Sequence of directories to scan.

        Returns:
            Combined tuple of all successfully parsed skills.
        """
        all_skills: list[Skill] = []
        for directory in directories:
            all_skills.extend(self.scan(directory))
        return tuple(all_skills)


def _find_skill_files(directory: Path) -> tuple[Path, ...]:
    """Recursively find all SKILL.md files under directory."""
    return tuple(sorted(directory.rglob(_SKILL_FILENAME)))


def _load_all(
    paths: tuple[Path, ...],
    parser: SkillParser,
) -> tuple[Skill, ...]:
    """Load all skill files, skipping failures."""
    skills: list[Skill] = []
    for path in paths:
        try:
            skill = parser.parse(path)
            skills.append(skill)
            logger.debug(
                "[SkillLoader] Loaded skill '%s' (format=%s) from %s",
                skill.skill_id,
                skill.source_format,
                path,
            )
        except Exception as exc:
            logger.error(
                f"[SkillLoader] Failed to parse {path}: {exc}",
                exc_info=True,
            )
    return tuple(skills)
