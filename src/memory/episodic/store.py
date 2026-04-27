"""Episodic memory store built on Markdown source files."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

import frontmatter

from src.common.content_scanner import scan
from src.memory.episodic.models import (
    Episode,
    EpisodeIndex,
    EpisodeSource,
    RelatedEntity,
)

logger = logging.getLogger(__name__)

EPISODES_DIR = "episodes"


# ---------------------------------------------------------------------------
# IndexFileLock
# ---------------------------------------------------------------------------


class IndexFileLock:
    """Process-internal async lock for episode writes.

    Ensures write_episode and write_episode_with_index execute serially within
    a single Worker process.

    Usage:
        lock = IndexFileLock()
        async with lock:
            write_episode(base_dir, episode)
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> None:
        await self._lock.acquire()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self._lock.release()


# ---------------------------------------------------------------------------
# Pure conversion functions
# ---------------------------------------------------------------------------


def episode_to_index(episode: Episode) -> EpisodeIndex:
    """Extract a flattened EpisodeIndex from an Episode for fast retrieval."""
    return EpisodeIndex(
        id=episode.episode_id,
        ts=episode.created_at,
        summary=episode.summary,
        entities=tuple(e.value for e in episode.related_entities),
        skills=(episode.source.skill_used,),
        duties=episode.related_duties,
        goals=episode.related_goals,
        score=episode.relevance_score,
    )


def episode_to_markdown(episode: Episode) -> str:
    """Serialize an Episode to YAML frontmatter + Markdown body."""
    meta: dict[str, Any] = {
        "episode_id": episode.episode_id,
        "created_at": episode.created_at,
        "source": asdict(episode.source),
        "related_entities": [asdict(e) for e in episode.related_entities],
        "related_goals": list(episode.related_goals),
        "related_duties": list(episode.related_duties),
        "relevance_score": episode.relevance_score,
        "last_retrieved": episode.last_retrieved,
        "retrieve_count": episode.retrieve_count,
    }

    body_parts = [f"# {episode.summary}", ""]
    if episode.key_findings:
        body_parts.append("## Key Findings")
        body_parts.append("")
        for finding in episode.key_findings:
            body_parts.append(f"- {finding}")
        body_parts.append("")

    post = frontmatter.Post(content="\n".join(body_parts), **meta)
    return frontmatter.dumps(post) + "\n"


def markdown_to_episode(content: str) -> Episode:
    """Deserialize a Markdown string (with YAML frontmatter) into an Episode."""
    post = frontmatter.loads(content)
    meta = post.metadata

    source_data = meta["source"]
    source = EpisodeSource(
        type=source_data["type"],
        skill_used=source_data["skill_used"],
        trigger=source_data.get("trigger"),
    )

    related_entities = tuple(
        RelatedEntity(type=e["type"], value=e["value"])
        for e in meta.get("related_entities", ())
    )

    # Parse body: extract summary from first heading, key_findings from bullets
    summary = _extract_summary(post.content, meta.get("episode_id", ""))
    key_findings = _extract_key_findings(post.content)

    return Episode(
        episode_id=meta["episode_id"],
        created_at=meta["created_at"],
        source=source,
        summary=summary,
        key_findings=key_findings,
        related_entities=related_entities,
        related_goals=tuple(meta.get("related_goals", ())),
        related_duties=tuple(meta.get("related_duties", ())),
        relevance_score=float(meta.get("relevance_score", 0.9)),
        last_retrieved=meta.get("last_retrieved"),
        retrieve_count=int(meta.get("retrieve_count", 0)),
    )


def _extract_summary(body: str, fallback: str) -> str:
    """Extract summary from the first Markdown heading."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return fallback


def _extract_key_findings(body: str) -> tuple[str, ...]:
    """Extract key findings from bullet list items after '## Key Findings'."""
    findings: list[str] = []
    in_section = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped == "## Key Findings":
            in_section = True
            continue
        if in_section:
            if stripped.startswith("## "):
                break
            if stripped.startswith("- "):
                findings.append(stripped[2:].strip())
    return tuple(findings)


# ---------------------------------------------------------------------------
# I/O functions
# ---------------------------------------------------------------------------


def write_episode(base_dir: Path, episode: Episode) -> Path:
    """Write an Episode as a Markdown file."""
    result = scan("\n".join((episode.summary, *episode.key_findings)))
    if not result.is_safe:
        raise ValueError(f"unsafe episode content: {', '.join(result.violations)}")

    episodes_dir = base_dir / EPISODES_DIR
    episodes_dir.mkdir(parents=True, exist_ok=True)

    md_path = episodes_dir / f"{episode.episode_id}.md"
    md_content = episode_to_markdown(episode)
    md_path.write_text(md_content, encoding="utf-8")

    logger.debug("Wrote episode %s to %s", episode.episode_id, md_path)
    return md_path


async def write_episode_with_index(
    base_dir: Path,
    episode: Episode,
    *,
    viking_indexer: Any | None = None,
) -> Path:
    """Write the Markdown source, then best-effort index it into OpenViking."""
    md_path = write_episode(base_dir, episode)
    if viking_indexer is None:
        return md_path
    try:
        await viking_indexer.index_episode(episode)
    except Exception as exc:
        logger.warning(
            "Viking index failed for %s: %s, will rebuild on next drift check",
            episode.episode_id,
            exc,
        )
    return md_path


def load_episode(base_dir: Path, episode_id: str) -> Episode:
    """Load a single Episode from its Markdown file.

    Raises FileNotFoundError if the episode does not exist.
    Read-only operation; no lock required.
    """
    md_path = base_dir / EPISODES_DIR / f"{episode_id}.md"
    content = md_path.read_text(encoding="utf-8")
    return markdown_to_episode(content)


def load_index(base_dir: Path) -> tuple[EpisodeIndex, ...]:
    """Load all episode indices by scanning the Markdown source files."""
    episodes_dir = base_dir / EPISODES_DIR
    if not episodes_dir.exists():
        return ()

    indices: list[EpisodeIndex] = []
    for md_file in sorted(episodes_dir.glob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8")
            indices.append(episode_to_index(markdown_to_episode(content)))
        except Exception as exc:
            logger.warning("Skipping invalid episode file %s: %s", md_file, exc)
    return tuple(sorted(indices, key=lambda item: item.ts))


def rebuild_index(base_dir: Path) -> tuple[EpisodeIndex, ...]:
    """Rebuild the derived in-memory index by scanning Markdown source files."""
    indices = load_index(base_dir)
    logger.info("Rebuilt derived episode index with %d entries", len(indices))
    return indices
