"""Shared rule store for cross-worker learning."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from src.common.content_scanner import scan

from .models import Rule, RuleScope, RuleSource
from .rule_manager import load_rules


SHARING_CONFIDENCE = 0.85
SHARING_APPLY_COUNT = 15


@dataclass(frozen=True)
class SharedRule:
    rule: Rule
    shared_by: str
    shared_at: str
    adoption_count: int = 0


def identify_sharable_rules(
    rules: tuple[Rule, ...],
    already_shared_ids: frozenset[str],
) -> tuple[Rule, ...]:
    """Select high-confidence active learned rules for tenant sharing."""
    return tuple(
        rule for rule in rules
        if rule.rule_id not in already_shared_ids
        and rule.type == "learned"
        and rule.status == "active"
        and rule.confidence >= SHARING_CONFIDENCE
        and rule.apply_count >= SHARING_APPLY_COUNT
        and not rule.scope.skills
    )


def propose_to_shared_store(
    rule: Rule,
    worker_id: str,
    shared_rules_dir: Path,
) -> SharedRule:
    """Persist one shared rule artifact after safety scanning."""
    result = scan(f"{rule.rule}\n{rule.reason}")
    if not result.is_safe:
        raise ValueError(f"unsafe shared rule: {', '.join(result.violations)}")

    shared = SharedRule(
        rule=rule,
        shared_by=worker_id,
        shared_at=datetime.now(timezone.utc).isoformat(),
        adoption_count=0,
    )
    shared_rules_dir.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(
        f"# {rule.rule}\n\n{rule.reason}",
        rule_id=rule.rule_id,
        shared_by=worker_id,
        shared_at=shared.shared_at,
        adoption_count=shared.adoption_count,
        category=rule.category,
        confidence=rule.confidence,
        apply_count=rule.apply_count,
        source_type=rule.source.type,
        source_evidence=rule.source.evidence,
        source_created_at=rule.source.created_at,
    )
    (shared_rules_dir / f"{rule.rule_id}.md").write_text(
        frontmatter.dumps(post),
        encoding="utf-8",
    )
    return shared


def load_shared_rules(shared_rules_dir: Path) -> tuple[SharedRule, ...]:
    """Load all shared rules from the tenant-level shared store."""
    if not shared_rules_dir.exists():
        return ()
    results: list[SharedRule] = []
    for path in sorted(shared_rules_dir.glob("*.md")):
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
        rule = Rule(
            rule_id=str(post.get("rule_id", path.stem)),
            type="learned",
            category=str(post.get("category", "strategy")),
            status="active",
            rule=_extract_heading(post.content),
            reason=_extract_reason(post.content),
            scope=RuleScope(),
            source=RuleSource(
                type=str(post.get("source_type", "self_reflection")),
                evidence=str(post.get("source_evidence", "")),
                created_at=str(post.get("source_created_at", "")),
            ),
            confidence=float(post.get("confidence", 0.5)),
            apply_count=int(post.get("apply_count", 0)),
        )
        results.append(
            SharedRule(
                rule=rule,
                shared_by=str(post.get("shared_by", "")),
                shared_at=str(post.get("shared_at", "")),
                adoption_count=int(post.get("adoption_count", 0)),
            )
        )
    return tuple(results)


def discover_adoptable_rules(
    shared_rules: tuple[SharedRule, ...],
    worker_rules: tuple[Rule, ...],
    worker_id: str,
) -> tuple[SharedRule, ...]:
    """Filter adoptable shared rules for one worker."""
    existing_ids = {rule.rule_id for rule in worker_rules}
    existing_sources = {rule.source.evidence for rule in worker_rules}
    return tuple(
        shared for shared in shared_rules
        if shared.shared_by != worker_id
        and shared.rule.rule_id not in existing_ids
        and shared.rule.rule_id not in existing_sources
    )


def adopt_shared_rule(
    shared_rule: SharedRule,
    worker_rules_dir: Path,
) -> Rule:
    """Copy one shared rule into the worker's learned rules."""
    adopted = replace(
        shared_rule.rule,
        rule_id=f"adopted-{shared_rule.rule.rule_id}",
        confidence=0.5,
        apply_count=0,
        source=RuleSource(
            type="cross_worker",
            evidence=shared_rule.shared_by,
            created_at=datetime.now(timezone.utc).isoformat(),
        ),
    )
    learned_dir = worker_rules_dir / "learned"
    learned_dir.mkdir(parents=True, exist_ok=True)
    from .models import rule_to_markdown

    (learned_dir / f"{adopted.rule_id}.md").write_text(
        rule_to_markdown(adopted),
        encoding="utf-8",
    )
    return adopted


def run_sharing_cycle(
    worker_id: str,
    worker_rules_dir: Path,
    shared_rules_dir: Path,
) -> tuple[SharedRule, ...]:
    """Persist newly shareable rules and return adoptable remote rules."""
    existing_shared = load_shared_rules(shared_rules_dir)
    existing_ids = frozenset(item.rule.rule_id for item in existing_shared)
    rules = load_rules(worker_rules_dir)
    for rule in identify_sharable_rules(rules, existing_ids):
        propose_to_shared_store(rule, worker_id, shared_rules_dir)
    updated_shared = load_shared_rules(shared_rules_dir)
    return discover_adoptable_rules(updated_shared, rules, worker_id)


def _extract_heading(content: str) -> str:
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return content.strip()


def _extract_reason(content: str) -> str:
    lines = content.splitlines()
    if len(lines) <= 1:
        return ""
    return "\n".join(lines[2:]).strip()
