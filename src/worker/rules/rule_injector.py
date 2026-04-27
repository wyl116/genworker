"""
Rule injector - selection and prompt formatting for rules.

Pure functions that filter and format rules for injection into LLM prompts.
Directives are always fully included; learned rules are filtered by
scope match and confidence, then ranked.
"""
from __future__ import annotations

from src.common.context_fence import fence_rules_context

from .models import Rule


# ---------------------------------------------------------------------------
# Rule selection
# ---------------------------------------------------------------------------

def select_rules(
    all_rules: tuple[Rule, ...],
    skill_id: str | None = None,
    max_results: int = 10,
) -> tuple[Rule, ...]:
    """
    Select rules for prompt injection.

    Strategy:
    1. All directives are always included (regardless of max_results).
    2. Learned rules are filtered by:
       - status == "active"
       - scope match (global scope or skill_id in scope.skills)
       - sorted by confidence descending
       - truncated to max_results
    """
    directives = tuple(r for r in all_rules if r.type == "directive")
    learned = _filter_learned(all_rules, skill_id, max_results)
    return directives + learned


def _filter_learned(
    all_rules: tuple[Rule, ...],
    skill_id: str | None,
    max_results: int,
) -> tuple[Rule, ...]:
    """Filter and rank learned rules by scope and confidence."""
    candidates = [
        r for r in all_rules
        if r.type == "learned"
        and r.status == "active"
        and _scope_matches(r, skill_id)
    ]
    candidates.sort(key=lambda r: r.confidence, reverse=True)
    return tuple(candidates[:max_results])


def _scope_matches(rule: Rule, skill_id: str | None) -> bool:
    """Check if a rule's scope matches the given skill_id."""
    if not rule.scope.skills:
        return True  # Global scope matches everything
    if skill_id is None:
        return True  # No skill filter means include all
    return skill_id in rule.scope.skills


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------

def format_for_prompt(rules: tuple[Rule, ...]) -> str:
    """
    Format selected rules into two sections for prompt injection:
    - [Admin Directives] section: directive rules (no confidence tag)
    - [Learned Rules] section: learned rules with [category/confidence] tags

    Returns empty string if no rules provided.
    """
    if not rules:
        return ""

    directives = [r for r in rules if r.type == "directive"]
    learned = [r for r in rules if r.type == "learned"]

    sections: list[str] = []

    if directives:
        lines = ["[Admin Directives]"]
        for r in directives:
            lines.append(f"- {r.rule}")
        sections.append("\n".join(lines))

    if learned:
        lines = ["[Learned Rules]"]
        for r in learned:
            tag = f"[{r.category}/{r.confidence:.2f}]"
            lines.append(f"- {tag} {r.rule}")
        sections.append(fence_rules_context("\n".join(lines)))

    return "\n\n".join(sections)
