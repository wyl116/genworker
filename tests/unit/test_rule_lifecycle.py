# edition: baseline
"""
Tests for rule lifecycle: CRUD, conflict detection, confidence management,
serialization, and rule generation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from src.worker.rules.models import (
    Rule,
    RuleCandidate,
    RuleScope,
    RuleSource,
    candidate_to_rule,
    markdown_to_rule,
    rule_to_markdown,
)
from src.worker.rules.rule_manager import (
    CONFIDENCE_BOOST,
    CONFIDENCE_PENALTY,
    MIN_CONFIDENCE_TO_ACTIVATE,
    create_rule,
    detect_conflict,
    load_rules,
    update_confidence,
)
from src.worker.rules.rule_generator import (
    extract_rule_from_feedback,
    extract_rule_from_reflection,
    validate_and_create_rule,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_source(
    type_: str = "user_feedback",
    evidence: str = "test evidence",
) -> RuleSource:
    return RuleSource(
        type=type_,
        evidence=evidence,
        created_at="2026-04-01T00:00:00+00:00",
    )


def _make_rule(
    rule_id: str = "rule-001",
    type_: str = "learned",
    category: str = "preference",
    status: str = "active",
    rule_text: str = "Always use JSON format",
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


def _make_candidate(
    rule_text: str = "Prefer concise answers",
    category: str = "preference",
    skills: tuple[str, ...] = (),
) -> RuleCandidate:
    return RuleCandidate(
        rule=rule_text,
        reason="Derived from feedback",
        category=category,
        scope=RuleScope(skills=skills),
        source=_make_source(),
    )


def _make_directive(
    rule_id: str = "dir-001",
    rule_text: str = "Always respond in Chinese",
    skills: tuple[str, ...] = (),
) -> Rule:
    return _make_rule(
        rule_id=rule_id,
        type_="directive",
        category="preference",
        rule_text=rule_text,
        confidence=1.0,
        skills=skills,
    )


def _setup_rules_dir(tmp_path: Path) -> Path:
    """Create a rules directory with directives/ and learned/ subdirs."""
    rules_dir = tmp_path / "rules"
    (rules_dir / "directives").mkdir(parents=True)
    (rules_dir / "learned").mkdir(parents=True)
    return rules_dir


# ---------------------------------------------------------------------------
# Serialization tests
# ---------------------------------------------------------------------------

class TestRuleSerialization:
    """Rule <-> Markdown round-trip tests."""

    def test_round_trip_basic(self):
        rule = _make_rule()
        md = rule_to_markdown(rule)
        restored = markdown_to_rule(md)

        assert restored.rule_id == rule.rule_id
        assert restored.type == rule.type
        assert restored.category == rule.category
        assert restored.status == rule.status
        assert restored.rule == rule.rule
        assert restored.reason == rule.reason
        assert restored.confidence == rule.confidence

    def test_round_trip_with_scope(self):
        rule = _make_rule(skills=("email", "calendar"))
        md = rule_to_markdown(rule)
        restored = markdown_to_rule(md)

        assert restored.scope.skills == ("email", "calendar")

    def test_round_trip_with_apply_count(self):
        from dataclasses import replace
        rule = replace(_make_rule(), apply_count=5, last_applied="2026-04-01")
        md = rule_to_markdown(rule)
        restored = markdown_to_rule(md)

        assert restored.apply_count == 5
        assert restored.last_applied == "2026-04-01"

    def test_round_trip_directive(self):
        rule = _make_directive()
        md = rule_to_markdown(rule)
        restored = markdown_to_rule(md)

        assert restored.type == "directive"
        assert restored.confidence == 1.0

    def test_invalid_markdown_raises(self):
        with pytest.raises(ValueError, match="missing YAML frontmatter"):
            markdown_to_rule("no frontmatter here")

    def test_candidate_to_rule(self):
        candidate = _make_candidate()
        rule = candidate_to_rule(candidate, "rule-test-123")

        assert rule.rule_id == "rule-test-123"
        assert rule.type == "learned"
        assert rule.status == "active"
        assert rule.confidence == 0.5
        assert rule.rule == candidate.rule


# ---------------------------------------------------------------------------
# Load rules tests
# ---------------------------------------------------------------------------

class TestLoadRules:

    def test_load_from_empty_dir(self, tmp_path):
        rules_dir = _setup_rules_dir(tmp_path)
        result = load_rules(rules_dir)
        assert result == ()

    def test_load_mixed_rules(self, tmp_path):
        rules_dir = _setup_rules_dir(tmp_path)

        directive = _make_directive()
        (rules_dir / "directives" / "dir-001.md").write_text(
            rule_to_markdown(directive),
        )

        learned = _make_rule()
        (rules_dir / "learned" / "rule-001.md").write_text(
            rule_to_markdown(learned),
        )

        result = load_rules(rules_dir)
        assert len(result) == 2
        types = {r.type for r in result}
        assert types == {"directive", "learned"}

    def test_load_skips_invalid_files(self, tmp_path):
        rules_dir = _setup_rules_dir(tmp_path)
        (rules_dir / "learned" / "bad.md").write_text("not valid frontmatter")

        good = _make_rule()
        (rules_dir / "learned" / "rule-001.md").write_text(
            rule_to_markdown(good),
        )

        result = load_rules(rules_dir)
        assert len(result) == 1

    def test_load_nonexistent_dir(self, tmp_path):
        result = load_rules(tmp_path / "nonexistent")
        assert result == ()


# ---------------------------------------------------------------------------
# Conflict detection tests
# ---------------------------------------------------------------------------

class TestDetectConflict:

    def test_no_conflict(self):
        candidate = _make_candidate(rule_text="Prefer JSON output")
        result = detect_conflict(
            candidate,
            principles=("Be helpful",),
            directives=(),
        )
        assert result is None

    def test_conflict_with_principle(self):
        candidate = _make_candidate(
            rule_text="Never use data validation",
            category="prohibition",
        )
        result = detect_conflict(
            candidate,
            principles=("Always use data validation",),
            directives=(),
        )
        assert result is not None
        assert "principle" in result.lower()

    def test_conflict_with_directive(self):
        candidate = _make_candidate(
            rule_text="Never use email for notifications",
            category="prohibition",
        )
        directive = _make_directive(
            rule_text="Always use email for notifications",
        )
        result = detect_conflict(
            candidate,
            principles=(),
            directives=(directive,),
        )
        assert result is not None
        assert "directive" in result.lower()

    def test_no_conflict_different_scope(self):
        candidate = _make_candidate(
            rule_text="Never use email for notifications",
            category="prohibition",
            skills=("calendar",),
        )
        directive = _make_directive(
            rule_text="Always use email for notifications",
            skills=("email",),
        )
        result = detect_conflict(
            candidate,
            principles=(),
            directives=(directive,),
        )
        # Different non-overlapping scopes -> no conflict
        assert result is None


# ---------------------------------------------------------------------------
# Rule creation tests
# ---------------------------------------------------------------------------

class TestCreateRule:

    def test_create_success(self, tmp_path):
        rules_dir = _setup_rules_dir(tmp_path)
        candidate = _make_candidate()
        result = create_rule(
            rules_dir, candidate,
            principles=(),
            existing_rules=(),
        )
        assert isinstance(result, Rule)
        assert result.type == "learned"
        assert result.confidence == 0.5
        # File should exist
        files = list((rules_dir / "learned").glob("*.md"))
        assert len(files) == 1

    def test_create_rejected_by_conflict(self, tmp_path):
        rules_dir = _setup_rules_dir(tmp_path)
        candidate = _make_candidate(
            rule_text="Never use data validation",
            category="prohibition",
        )
        result = create_rule(
            rules_dir, candidate,
            principles=("Always use data validation",),
            existing_rules=(),
        )
        assert isinstance(result, str)
        assert "conflict" in result.lower() or "principle" in result.lower()

    def test_create_evicts_lowest_confidence(self, tmp_path):
        rules_dir = _setup_rules_dir(tmp_path)

        # Create existing rules at capacity
        existing = tuple(
            _make_rule(
                rule_id=f"rule-{i:03d}",
                confidence=0.3 + (i * 0.01),
            )
            for i in range(3)
        )
        for r in existing:
            (rules_dir / "learned" / f"{r.rule_id}.md").write_text(
                rule_to_markdown(r),
            )

        candidate = _make_candidate(rule_text="New important rule")
        result = create_rule(
            rules_dir, candidate,
            principles=(),
            existing_rules=existing,
            max_learned_rules=3,
        )
        assert isinstance(result, Rule)

        # The lowest-confidence rule should be suspended
        evicted_content = (rules_dir / "learned" / "rule-000.md").read_text()
        evicted = markdown_to_rule(evicted_content)
        assert evicted.status == "suspended"


# ---------------------------------------------------------------------------
# Confidence update tests
# ---------------------------------------------------------------------------

class TestUpdateConfidence:

    def test_boost(self, tmp_path):
        rules_dir = _setup_rules_dir(tmp_path)
        rule = _make_rule(confidence=0.5)
        (rules_dir / "learned" / f"{rule.rule_id}.md").write_text(
            rule_to_markdown(rule),
        )

        updated = update_confidence(rules_dir, rule.rule_id, CONFIDENCE_BOOST)
        assert updated.confidence == pytest.approx(0.55)
        assert updated.status == "active"

    def test_penalty(self, tmp_path):
        rules_dir = _setup_rules_dir(tmp_path)
        rule = _make_rule(confidence=0.5)
        (rules_dir / "learned" / f"{rule.rule_id}.md").write_text(
            rule_to_markdown(rule),
        )

        updated = update_confidence(rules_dir, rule.rule_id, CONFIDENCE_PENALTY)
        assert updated.confidence == pytest.approx(0.4)

    def test_clamp_to_zero(self, tmp_path):
        rules_dir = _setup_rules_dir(tmp_path)
        rule = _make_rule(confidence=0.05)
        (rules_dir / "learned" / f"{rule.rule_id}.md").write_text(
            rule_to_markdown(rule),
        )

        updated = update_confidence(rules_dir, rule.rule_id, -0.5)
        assert updated.confidence == 0.0
        assert updated.status == "suspended"

    def test_clamp_to_one(self, tmp_path):
        rules_dir = _setup_rules_dir(tmp_path)
        rule = _make_rule(confidence=0.95)
        (rules_dir / "learned" / f"{rule.rule_id}.md").write_text(
            rule_to_markdown(rule),
        )

        updated = update_confidence(rules_dir, rule.rule_id, 0.5)
        assert updated.confidence == 1.0

    def test_suspend_below_threshold(self, tmp_path):
        rules_dir = _setup_rules_dir(tmp_path)
        rule = _make_rule(confidence=0.35)
        (rules_dir / "learned" / f"{rule.rule_id}.md").write_text(
            rule_to_markdown(rule),
        )

        updated = update_confidence(rules_dir, rule.rule_id, CONFIDENCE_PENALTY)
        assert updated.confidence == pytest.approx(0.25)
        assert updated.status == "suspended"

    def test_nonexistent_rule_raises(self, tmp_path):
        rules_dir = _setup_rules_dir(tmp_path)
        with pytest.raises(FileNotFoundError):
            update_confidence(rules_dir, "nonexistent", 0.1)


# ---------------------------------------------------------------------------
# Rule generator tests (LLM extraction)
# ---------------------------------------------------------------------------

@dataclass
class _MockLLMResponse:
    """Mock LLM response for testing."""
    content: str = ""


class _MockLLMClient:
    """Mock LLM client that returns predefined responses."""

    def __init__(self, response_content: str):
        self._response = _MockLLMResponse(content=response_content)

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        system_blocks: list[dict[str, Any]] | None = None,
        intent=None,
    ) -> _MockLLMResponse:
        return self._response


class TestExtractRuleFromFeedback:

    @pytest.mark.asyncio
    async def test_extract_valid_rule(self):
        response = json.dumps({
            "rule": "Always format dates as ISO 8601",
            "reason": "User prefers consistent date formats",
            "category": "preference",
        })
        client = _MockLLMClient(response)
        result = await extract_rule_from_feedback(
            "Use ISO dates", "date task", client,
        )
        assert result is not None
        assert result.rule == "Always format dates as ISO 8601"
        assert result.category == "preference"
        assert result.source.type == "user_feedback"

    @pytest.mark.asyncio
    async def test_extract_no_rule(self):
        response = json.dumps({"no_rule": True})
        client = _MockLLMClient(response)
        result = await extract_rule_from_feedback(
            "thanks", "simple task", client,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_extract_invalid_json(self):
        client = _MockLLMClient("not json at all")
        result = await extract_rule_from_feedback(
            "feedback", "context", client,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_extract_from_code_block(self):
        response = '```json\n{"rule": "Check inputs", "reason": "Safety", "category": "strategy"}\n```'
        client = _MockLLMClient(response)
        result = await extract_rule_from_feedback(
            "validate more", "api task", client,
        )
        assert result is not None
        assert result.rule == "Check inputs"
        assert result.category == "strategy"


class TestExtractRuleFromReflection:

    @pytest.mark.asyncio
    async def test_extract_valid_reflection(self):
        response = json.dumps({
            "rule": "Break complex tasks into subtasks",
            "reason": "Improves success rate for multi-step tasks",
            "category": "strategy",
        })
        client = _MockLLMClient(response)
        result = await extract_rule_from_reflection(
            "completed 3 steps", "good", client,
        )
        assert result is not None
        assert result.rule == "Break complex tasks into subtasks"
        assert result.source.type == "self_reflection"

    @pytest.mark.asyncio
    async def test_extract_no_reflection_rule(self):
        response = json.dumps({"no_rule": True})
        client = _MockLLMClient(response)
        result = await extract_rule_from_reflection(
            "simple task done", "perfect", client,
        )
        assert result is None


class TestValidateAndCreateRule:

    @pytest.mark.asyncio
    async def test_validate_and_create_success(self, tmp_path):
        rules_dir = _setup_rules_dir(tmp_path)
        candidate = _make_candidate()
        result = await validate_and_create_rule(
            candidate,
            rules_dir=rules_dir,
            principles=(),
            existing_rules=(),
        )
        assert isinstance(result, Rule)

    @pytest.mark.asyncio
    async def test_validate_and_create_conflict(self, tmp_path):
        rules_dir = _setup_rules_dir(tmp_path)
        candidate = _make_candidate(
            rule_text="Never use data validation",
            category="prohibition",
        )
        result = await validate_and_create_rule(
            candidate,
            rules_dir=rules_dir,
            principles=("Always use data validation",),
            existing_rules=(),
        )
        assert isinstance(result, str)
