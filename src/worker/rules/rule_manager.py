"""
Rule manager - CRUD, conflict detection, and confidence lifecycle.

Pure functions for rule management operations on the filesystem.
Rules are stored as Markdown files with YAML frontmatter in:
  - rules/directives/  (admin-managed, always included)
  - rules/learned/     (self-evolved, confidence-gated)
"""
from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from .models import (
    Rule,
    RuleCandidate,
    candidate_to_rule,
    markdown_to_rule,
    rule_to_markdown,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Confidence lifecycle constants
# ---------------------------------------------------------------------------

CONFIDENCE_BOOST: float = 0.05
CONFIDENCE_PENALTY: float = -0.1
CONFIDENCE_DECAY_PER_30D: float = 0.05
MIN_CONFIDENCE_TO_ACTIVATE: float = 0.3


# ---------------------------------------------------------------------------
# Rule loading
# ---------------------------------------------------------------------------

def load_rules(rules_dir: Path) -> tuple[Rule, ...]:
    """
    Scan rules/directives/ and rules/learned/ for .md files,
    parse each into a Rule. Parsing failures are logged and skipped.
    """
    results: list[Rule] = []
    for sub in ("directives", "learned"):
        sub_dir = rules_dir / sub
        if not sub_dir.is_dir():
            continue
        for md_file in sorted(sub_dir.glob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8")
                rule = markdown_to_rule(content)
                results.append(rule)
            except Exception as exc:
                logger.warning(
                    "Failed to parse rule file %s: %s", md_file, exc,
                )
    return tuple(results)


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------

def detect_conflict(
    candidate: RuleCandidate,
    principles: tuple[str, ...],
    directives: tuple[Rule, ...],
) -> str | None:
    """
    Detect conflicts between a candidate rule and existing principles/directives.

    Heuristic checks:
    1. Prohibition candidates contradicting a principle keyword
    2. Same-scope directive with opposing semantics

    Returns a conflict description string, or None if no conflict.
    """
    candidate_lower = candidate.rule.lower()

    # Check against principles
    for principle in principles:
        principle_lower = principle.lower()
        if _has_semantic_opposition(candidate_lower, principle_lower):
            return (
                f"Candidate conflicts with principle: '{principle}'"
            )

    # Check against directives with overlapping scope
    for directive in directives:
        if directive.type != "directive":
            continue
        if not _scopes_overlap(candidate.scope.skills, directive.scope.skills):
            continue
        if _has_semantic_opposition(candidate_lower, directive.rule.lower()):
            return (
                f"Candidate conflicts with directive '{directive.rule_id}': "
                f"'{directive.rule}'"
            )

    return None


def _has_semantic_opposition(text_a: str, text_b: str) -> bool:
    """
    Heuristic: detect if two rule texts have opposing semantics.

    Checks for negation patterns: one text says "never/do not/prohibit X"
    while the other says "always/must/should X" for overlapping keywords.
    """
    negative_markers = ("never", "do not", "don't", "prohibit", "forbid", "avoid")
    positive_markers = ("always", "must", "should", "ensure", "require")

    a_negative = any(m in text_a for m in negative_markers)
    b_negative = any(m in text_b for m in negative_markers)
    a_positive = any(m in text_a for m in positive_markers)
    b_positive = any(m in text_b for m in positive_markers)

    # One is negative, the other positive, and they share keywords
    if (a_negative and b_positive) or (a_positive and b_negative):
        a_words = set(text_a.split())
        b_words = set(text_b.split())
        shared = a_words & b_words
        # Filter out common stop words and markers
        stop_words = {
            "the", "a", "an", "is", "are", "to", "and", "or", "in",
            "of", "for", "with", "on", "at", "by", "not", "do",
        } | set(negative_markers) | set(positive_markers)
        meaningful_shared = shared - stop_words
        if meaningful_shared:
            return True
    return False


def _scopes_overlap(
    skills_a: tuple[str, ...],
    skills_b: tuple[str, ...],
) -> bool:
    """Two scopes overlap if either is global (empty) or they share a skill."""
    if not skills_a or not skills_b:
        return True
    return bool(set(skills_a) & set(skills_b))


# ---------------------------------------------------------------------------
# Rule creation
# ---------------------------------------------------------------------------

def create_rule(
    rules_dir: Path,
    candidate: RuleCandidate,
    principles: tuple[str, ...],
    existing_rules: tuple[Rule, ...],
    max_learned_rules: int = 30,
) -> Rule | str:
    """
    Create a new learned rule: conflict check -> capacity check -> write.

    Returns the created Rule on success, or a conflict description string.
    """
    directives = tuple(r for r in existing_rules if r.type == "directive")
    conflict = detect_conflict(candidate, principles, directives)
    if conflict is not None:
        return conflict

    learned = tuple(
        r for r in existing_rules
        if r.type == "learned" and r.status == "active"
    )

    # Evict lowest-confidence rule if at capacity
    if len(learned) >= max_learned_rules:
        _evict_lowest_confidence(rules_dir, learned)

    rule = candidate_to_rule(candidate)
    _write_rule(rules_dir, rule)
    return rule


def _evict_lowest_confidence(
    rules_dir: Path,
    learned_rules: tuple[Rule, ...],
) -> None:
    """Suspend the learned rule with the lowest confidence."""
    if not learned_rules:
        return
    lowest = min(learned_rules, key=lambda r: r.confidence)
    suspended = replace(lowest, status="suspended")
    _write_rule(rules_dir, suspended)
    logger.info(
        "Evicted rule %s (confidence=%.2f) to make room",
        lowest.rule_id, lowest.confidence,
    )


def _write_rule(rules_dir: Path, rule: Rule) -> Path:
    """Write a rule to the appropriate subdirectory."""
    sub = "directives" if rule.type == "directive" else "learned"
    target_dir = rules_dir / sub
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / f"{rule.rule_id}.md"
    file_path.write_text(rule_to_markdown(rule), encoding="utf-8")
    return file_path


# ---------------------------------------------------------------------------
# Confidence update
# ---------------------------------------------------------------------------

def update_confidence(
    rules_dir: Path,
    rule_id: str,
    delta: float,
) -> Rule:
    """
    Update a rule's confidence by delta, clamping to [0, 1].
    Suspends the rule if confidence falls below threshold.
    Returns the updated Rule.
    """
    learned_dir = rules_dir / "learned"
    file_path = learned_dir / f"{rule_id}.md"

    if not file_path.exists():
        raise FileNotFoundError(f"Rule file not found: {file_path}")

    content = file_path.read_text(encoding="utf-8")
    rule = markdown_to_rule(content)

    new_confidence = max(0.0, min(1.0, rule.confidence + delta))
    new_status = (
        "suspended"
        if new_confidence < MIN_CONFIDENCE_TO_ACTIVATE
        else rule.status
    )

    updated = replace(rule, confidence=new_confidence, status=new_status)
    file_path.write_text(rule_to_markdown(updated), encoding="utf-8")
    return updated
