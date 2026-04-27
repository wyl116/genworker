"""Episodic memory system built on Markdown source files."""
import importlib

_EXPORTS = {
    "Episode": ".models",
    "EpisodeIndex": ".models",
    "EpisodeQuery": ".models",
    "EpisodeSource": ".models",
    "RelatedEntity": ".models",
    "IndexFileLock": ".store",
    "episode_to_index": ".store",
    "episode_to_markdown": ".store",
    "markdown_to_episode": ".store",
    "write_episode": ".store",
    "write_episode_with_index": ".store",
    "load_episode": ".store",
    "load_index": ".store",
    "rebuild_index": ".store",
    "compute_decayed_score": ".decay",
    "identify_archive_candidates": ".decay",
    "run_decay_cycle": ".decay",
    "EpisodeRuleLink": ".linkage",
    "create_links": ".linkage",
    "write_linkage": ".linkage",
    "load_linkage": ".linkage",
    "apply_outcome_feedback": ".linkage",
    "compute_rule_effectiveness": ".linkage",
}


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module = importlib.import_module(_EXPORTS[name], __name__)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))

__all__ = [
    "Episode",
    "EpisodeIndex",
    "EpisodeQuery",
    "EpisodeSource",
    "RelatedEntity",
    "IndexFileLock",
    "episode_to_index",
    "episode_to_markdown",
    "markdown_to_episode",
    "write_episode",
    "write_episode_with_index",
    "load_episode",
    "load_index",
    "rebuild_index",
    "compute_decayed_score",
    "identify_archive_candidates",
    "run_decay_cycle",
    "EpisodeRuleLink",
    "create_links",
    "write_linkage",
    "load_linkage",
    "apply_outcome_feedback",
    "compute_rule_effectiveness",
]
