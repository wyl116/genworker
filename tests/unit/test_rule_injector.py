# edition: baseline
"""
Tests for rule injector - selection and prompt formatting.
"""
from __future__ import annotations

import pytest

from src.worker.rules.models import Rule, RuleScope, RuleSource
from src.worker.rules.rule_injector import format_for_prompt, select_rules


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_source() -> RuleSource:
    return RuleSource(
        type="user_feedback",
        evidence="test",
        created_at="2026-04-01T00:00:00+00:00",
    )


def _make_rule(
    rule_id: str = "rule-001",
    type_: str = "learned",
    category: str = "preference",
    status: str = "active",
    rule_text: str = "Use JSON format",
    confidence: float = 0.7,
    skills: tuple[str, ...] = (),
) -> Rule:
    return Rule(
        rule_id=rule_id,
        type=type_,
        category=category,
        status=status,
        rule=rule_text,
        reason="Test reason",
        scope=RuleScope(skills=skills),
        source=_make_source(),
        confidence=confidence,
    )


def _make_directive(
    rule_id: str = "dir-001",
    rule_text: str = "Respond in Chinese",
) -> Rule:
    return _make_rule(
        rule_id=rule_id,
        type_="directive",
        rule_text=rule_text,
        confidence=1.0,
    )


# ---------------------------------------------------------------------------
# Selection tests
# ---------------------------------------------------------------------------

class TestSelectRules:

    def test_empty_rules(self):
        result = select_rules((), skill_id=None)
        assert result == ()

    def test_directives_always_included(self):
        d1 = _make_directive("dir-001", "Rule A")
        d2 = _make_directive("dir-002", "Rule B")
        result = select_rules((d1, d2), skill_id=None, max_results=0)
        assert len(result) == 2  # directives bypass max_results

    def test_learned_rules_sorted_by_confidence(self):
        r1 = _make_rule(rule_id="r1", confidence=0.5)
        r2 = _make_rule(rule_id="r2", confidence=0.9)
        r3 = _make_rule(rule_id="r3", confidence=0.7)

        result = select_rules((r1, r2, r3), skill_id=None, max_results=10)
        learned = [r for r in result if r.type == "learned"]
        assert learned[0].rule_id == "r2"
        assert learned[1].rule_id == "r3"
        assert learned[2].rule_id == "r1"

    def test_learned_rules_truncated_to_max(self):
        rules = tuple(
            _make_rule(rule_id=f"r{i}", confidence=0.5 + i * 0.01)
            for i in range(5)
        )
        result = select_rules(rules, skill_id=None, max_results=2)
        learned = [r for r in result if r.type == "learned"]
        assert len(learned) == 2

    def test_suspended_rules_excluded(self):
        active = _make_rule(rule_id="r1", status="active")
        suspended = _make_rule(rule_id="r2", status="suspended")

        result = select_rules((active, suspended), skill_id=None)
        learned = [r for r in result if r.type == "learned"]
        assert len(learned) == 1
        assert learned[0].rule_id == "r1"

    def test_scope_filtering_by_skill(self):
        global_rule = _make_rule(rule_id="r1", skills=())
        email_rule = _make_rule(rule_id="r2", skills=("email",))
        cal_rule = _make_rule(rule_id="r3", skills=("calendar",))

        result = select_rules(
            (global_rule, email_rule, cal_rule),
            skill_id="email",
        )
        learned_ids = {r.rule_id for r in result if r.type == "learned"}
        assert "r1" in learned_ids  # global always matches
        assert "r2" in learned_ids  # email matches
        assert "r3" not in learned_ids  # calendar doesn't match

    def test_no_skill_filter_includes_all(self):
        scoped = _make_rule(rule_id="r1", skills=("email",))
        result = select_rules((scoped,), skill_id=None)
        assert len(result) == 1

    def test_mixed_directives_and_learned(self):
        d = _make_directive()
        r = _make_rule()
        result = select_rules((d, r), skill_id=None)
        assert len(result) == 2
        types = {r_.type for r_ in result}
        assert types == {"directive", "learned"}


# ---------------------------------------------------------------------------
# Formatting tests
# ---------------------------------------------------------------------------

class TestFormatForPrompt:

    def test_empty_rules(self):
        result = format_for_prompt(())
        assert result == ""

    def test_directives_only(self):
        d1 = _make_directive("dir-001", "Rule A")
        d2 = _make_directive("dir-002", "Rule B")
        result = format_for_prompt((d1, d2))

        assert "[Admin Directives]" in result
        assert "- Rule A" in result
        assert "- Rule B" in result
        assert "[Learned Rules]" not in result

    def test_learned_only(self):
        r = _make_rule(category="strategy", confidence=0.75)
        result = format_for_prompt((r,))

        assert "<learned-rules>" in result
        assert "[Learned Rules]" in result
        assert "[strategy/0.75]" in result
        assert "[Admin Directives]" not in result

    def test_mixed_formatting(self):
        d = _make_directive(rule_text="Admin directive text")
        r = _make_rule(
            rule_text="Learned rule text",
            category="preference",
            confidence=0.80,
        )
        result = format_for_prompt((d, r))

        assert "[Admin Directives]" in result
        assert "- Admin directive text" in result
        assert "[Learned Rules]" in result
        assert "[preference/0.80]" in result
        assert "- [preference/0.80] Learned rule text" in result

    def test_learned_rules_wrapped_in_fence(self):
        result = format_for_prompt((_make_rule(),))
        assert "<learned-rules>" in result
        assert "</learned-rules>" in result

    def test_confidence_format_precision(self):
        r = _make_rule(confidence=0.333)
        result = format_for_prompt((r,))
        # Should be formatted to 2 decimal places
        assert "[preference/0.33]" in result

    def test_directives_appear_before_learned(self):
        d = _make_directive(rule_text="Directive")
        r = _make_rule(rule_text="Learned")
        result = format_for_prompt((d, r))

        dir_pos = result.index("[Admin Directives]")
        learned_pos = result.index("[Learned Rules]")
        assert dir_pos < learned_pos
