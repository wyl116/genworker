"""
Rule data models - frozen dataclasses for the self-evolving rules subsystem.

Defines:
- RuleSource: origin of a rule (user feedback, self-reflection, admin)
- RuleScope: applicability constraints (skill IDs)
- Rule: the core rule entity with confidence lifecycle
- RuleCandidate: pre-validation candidate before becoming a Rule
- RuleQuery: filtering parameters for rule retrieval

All models use @dataclass(frozen=True) for immutability.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RuleSource:
    """Origin metadata for a rule."""
    type: str              # "user_feedback" | "self_reflection" | "admin"
    evidence: str
    created_at: str        # ISO 8601


@dataclass(frozen=True)
class RuleScope:
    """Applicability constraints for a rule."""
    skills: tuple[str, ...] = ()  # Applicable skill IDs (empty = global)


@dataclass(frozen=True)
class Rule:
    """
    Core rule entity with confidence-based lifecycle.

    Immutable - use dataclasses.replace() for mutations.
    """
    rule_id: str
    type: str              # "learned" | "directive"
    category: str          # "preference" | "strategy" | "prohibition"
    status: str            # "active" | "suspended" | "archived" | "crystallized"
    rule: str              # Rule content text
    reason: str            # Why this rule exists
    scope: RuleScope
    source: RuleSource
    confidence: float      # 0.0 ~ 1.0
    last_applied: str | None = None
    apply_count: int = 0


@dataclass(frozen=True)
class RuleQuery:
    """Filtering parameters for rule retrieval."""
    skill_id: str | None = None
    category: str | None = None
    min_confidence: float = 0.0
    max_results: int = 10


@dataclass(frozen=True)
class RuleCandidate:
    """
    Pre-validation rule candidate.

    Must pass conflict detection before becoming a formal Rule.
    """
    rule: str
    reason: str
    category: str
    scope: RuleScope
    source: RuleSource


# ---------------------------------------------------------------------------
# Serialization constants
# ---------------------------------------------------------------------------

_FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$",
    re.DOTALL,
)

_INITIAL_CONFIDENCE = 0.5


# ---------------------------------------------------------------------------
# Serialization functions
# ---------------------------------------------------------------------------

def rule_to_markdown(rule: Rule) -> str:
    """Serialize a Rule to YAML frontmatter + Markdown body."""
    frontmatter: dict[str, Any] = {
        "rule_id": rule.rule_id,
        "type": rule.type,
        "category": rule.category,
        "status": rule.status,
        "confidence": rule.confidence,
        "scope": {"skills": list(rule.scope.skills)} if rule.scope.skills else {},
        "source": {
            "type": rule.source.type,
            "evidence": rule.source.evidence,
            "created_at": rule.source.created_at,
        },
    }
    if rule.last_applied is not None:
        frontmatter["last_applied"] = rule.last_applied
    if rule.apply_count > 0:
        frontmatter["apply_count"] = rule.apply_count

    yaml_str = yaml.dump(
        frontmatter,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    ).rstrip("\n")

    body = f"# {rule.rule}\n\n{rule.reason}"
    return f"---\n{yaml_str}\n---\n{body}\n"


def markdown_to_rule(content: str) -> Rule:
    """Deserialize a YAML-frontmatter Markdown string into a Rule."""
    match = _FRONTMATTER_PATTERN.match(content)
    if not match:
        raise ValueError("Invalid rule markdown: missing YAML frontmatter")

    yaml_section = match.group(1)
    body_section = match.group(2).strip()

    meta = yaml.safe_load(yaml_section)
    if not isinstance(meta, dict):
        raise ValueError("Invalid rule markdown: frontmatter is not a mapping")

    # Parse body: first heading line is rule text, rest is reason
    lines = body_section.split("\n", 1)
    rule_text = lines[0].lstrip("# ").strip() if lines else ""
    reason_text = lines[1].strip() if len(lines) > 1 else ""

    scope_data = meta.get("scope", {})
    skills_raw = scope_data.get("skills", ()) if isinstance(scope_data, dict) else ()
    scope = RuleScope(skills=tuple(skills_raw))

    source_data = meta.get("source", {})
    source = RuleSource(
        type=source_data.get("type", "admin"),
        evidence=source_data.get("evidence", ""),
        created_at=source_data.get("created_at", ""),
    )

    return Rule(
        rule_id=meta.get("rule_id", ""),
        type=meta.get("type", "learned"),
        category=meta.get("category", "preference"),
        status=meta.get("status", "active"),
        rule=rule_text,
        reason=reason_text,
        scope=scope,
        source=source,
        confidence=float(meta.get("confidence", 0.5)),
        last_applied=meta.get("last_applied"),
        apply_count=int(meta.get("apply_count", 0)),
    )


def candidate_to_rule(candidate: RuleCandidate, rule_id: str | None = None) -> Rule:
    """Convert a RuleCandidate to a formal Rule with initial confidence."""
    generated_id = rule_id or f"rule-{uuid.uuid4().hex[:8]}"
    return Rule(
        rule_id=generated_id,
        type="learned",
        category=candidate.category,
        status="active",
        rule=candidate.rule,
        reason=candidate.reason,
        scope=candidate.scope,
        source=candidate.source,
        confidence=_INITIAL_CONFIDENCE,
    )
