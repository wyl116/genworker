"""Episode-rule linkage and outcome feedback."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from src.memory.episodic.models import EpisodeIndex
from src.memory.episodic.store import (
    episode_to_markdown,
    load_episode,
    rebuild_index,
)
from src.worker.rules.rule_manager import CONFIDENCE_BOOST, CONFIDENCE_PENALTY, update_confidence


LINKAGE_FILENAME = "rule_linkage.jsonl"
FEEDBACK_FILENAME = "feedback_log.jsonl"


@dataclass(frozen=True)
class EpisodeRuleLink:
    """Link between one episode and one applied rule."""

    episode_id: str
    rule_id: str
    linked_at: str


def create_links(
    episode_id: str,
    applied_rule_ids: tuple[str, ...],
    linked_at: str,
) -> tuple[EpisodeRuleLink, ...]:
    """Create immutable links for all applied rules."""
    return tuple(
        EpisodeRuleLink(
            episode_id=episode_id,
            rule_id=rule_id,
            linked_at=linked_at,
        )
        for rule_id in applied_rule_ids
    )


def write_linkage(memory_dir: Path, links: tuple[EpisodeRuleLink, ...]) -> None:
    """Append linkage rows to ``rule_linkage.jsonl``."""
    if not links:
        return
    memory_dir.mkdir(parents=True, exist_ok=True)
    path = memory_dir / LINKAGE_FILENAME
    with path.open("a", encoding="utf-8") as handle:
        for link in links:
            handle.write(json.dumps(link.__dict__, ensure_ascii=False) + "\n")


def load_linkage(memory_dir: Path) -> tuple[EpisodeRuleLink, ...]:
    """Load linkage rows from disk."""
    path = memory_dir / LINKAGE_FILENAME
    if not path.exists():
        return ()
    results: list[EpisodeRuleLink] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        results.append(
            EpisodeRuleLink(
                episode_id=str(data["episode_id"]),
                rule_id=str(data["rule_id"]),
                linked_at=str(data["linked_at"]),
            )
        )
    return tuple(results)


async def apply_outcome_feedback(
    episode_id: str,
    outcome: str,
    memory_dir: Path,
    rules_dir: Path,
) -> tuple[str, ...]:
    """Apply confidence and relevance feedback once per episode/outcome."""
    if _feedback_exists(memory_dir, episode_id, outcome):
        return ()

    links = tuple(link for link in load_linkage(memory_dir) if link.episode_id == episode_id)
    if not links:
        _write_feedback_record(memory_dir, episode_id, outcome, ())
        return ()

    delta = CONFIDENCE_BOOST if outcome == "success" else CONFIDENCE_PENALTY
    updated_rule_ids: list[str] = []
    for link in links:
        try:
            update_confidence(rules_dir, link.rule_id, delta)
            updated_rule_ids.append(link.rule_id)
        except FileNotFoundError:
            continue

    _update_episode_relevance(memory_dir, episode_id, delta)
    _write_feedback_record(memory_dir, episode_id, outcome, tuple(updated_rule_ids))
    return tuple(updated_rule_ids)


def compute_rule_effectiveness(
    linkage: tuple[EpisodeRuleLink, ...],
    episodes: tuple[EpisodeIndex, ...],
) -> dict[str, float]:
    """Compute success correlation for each linked rule."""
    episode_map = {episode.id: episode for episode in episodes}
    stats: dict[str, list[int]] = {}
    for link in linkage:
        episode = episode_map.get(link.episode_id)
        if episode is None:
            continue
        total, success = stats.get(link.rule_id, [0, 0])
        total += 1
        if _episode_looks_successful(episode):
            success += 1
        stats[link.rule_id] = [total, success]
    return {
        rule_id: (success / total if total else 0.0)
        for rule_id, (total, success) in stats.items()
    }


def _update_episode_relevance(memory_dir: Path, episode_id: str, delta: float) -> None:
    try:
        episode = load_episode(memory_dir, episode_id)
    except FileNotFoundError:
        return
    updated = replace(
        episode,
        relevance_score=max(0.0, min(1.0, episode.relevance_score + delta)),
    )
    episode_path = memory_dir / "episodes" / f"{episode_id}.md"
    episode_path.write_text(episode_to_markdown(updated), encoding="utf-8")
    rebuild_index(memory_dir)


def _feedback_exists(memory_dir: Path, episode_id: str, outcome: str) -> bool:
    path = memory_dir / FEEDBACK_FILENAME
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if data.get("episode_id") == episode_id and data.get("outcome") == outcome:
            return True
    return False


def _write_feedback_record(
    memory_dir: Path,
    episode_id: str,
    outcome: str,
    rule_ids: tuple[str, ...],
) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    path = memory_dir / FEEDBACK_FILENAME
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "episode_id": episode_id,
                    "outcome": outcome,
                    "rule_ids": list(rule_ids),
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def _episode_looks_successful(episode: EpisodeIndex) -> bool:
    summary = episode.summary.lower()
    return "task failed" not in summary and "failure" not in summary and "error:" not in summary
