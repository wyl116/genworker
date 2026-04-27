# edition: baseline
"""
Integration tests for skill matching, registry, and directory scanning.

Tests:
- Keyword weighted matching with scoring
- LLM fallback (mocked)
- Fallback to default_skill
- Worker skill overrides tenant/system
- Multi-level directory scanning
- Error tolerance during loading
"""
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence
from unittest.mock import AsyncMock

import pytest

from src.skills.loader import SkillLoader
from src.skills.matcher import MatchResult, MatchStatus, SkillMatcher
from src.skills.models import (
    Skill,
    SkillKeyword,
    SkillScope,
    SkillStrategy,
    StrategyMode,
)
from src.skills.parser import SkillParser
from src.skills.registry import SkillRegistry


# --- Helpers ---

def _make_skill(
    skill_id: str,
    name: str = "",
    description: str = "",
    scope: SkillScope = SkillScope.SYSTEM,
    priority: int = 0,
    keywords: tuple[SkillKeyword, ...] = (),
    default_skill: bool = False,
) -> Skill:
    """Create a test skill with minimal required fields."""
    return Skill(
        skill_id=skill_id,
        name=name or skill_id,
        description=description,
        scope=scope,
        priority=priority,
        keywords=keywords,
        default_skill=default_skill,
    )


def _write_skill_md(directory: Path, skill_id: str, content: str) -> Path:
    """Write a SKILL.md file under a skill subdirectory."""
    skill_dir = directory / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(content, encoding="utf-8")
    return skill_path


ANALYSIS_SKILL_MD = """\
---
skill_id: "data-analysis"
name: "数据分析"
scope: "system"
priority: 10

strategy:
  mode: "hybrid"

keywords:
  - { keyword: "数据分析", weight: 1.0 }
  - { keyword: "销售趋势", weight: 0.8 }

recommended_tools:
  - "sql_executor"

default_skill: false
---

## instructions.general
数据分析指令
"""

GENERAL_SKILL_MD = """\
---
skill_id: "general-query"
name: "通用查询"
scope: "system"
priority: 0

strategy:
  mode: "autonomous"

keywords:
  - { keyword: "帮我", weight: 0.3 }

default_skill: true
---

## instructions.general
通用查询指令
"""

TENANT_ANALYSIS_SKILL_MD = """\
---
skill_id: "data-analysis"
name: "租户数据分析"
scope: "tenant"
priority: 10

strategy:
  mode: "hybrid"

keywords:
  - { keyword: "数据分析", weight: 1.2 }
  - { keyword: "租户专属", weight: 0.9 }

default_skill: false
---

## instructions.general
租户级数据分析指令
"""

WORKER_ANALYSIS_SKILL_MD = """\
---
skill_id: "data-analysis"
name: "Worker数据分析"
scope: "worker"
priority: 10

strategy:
  mode: "deterministic"

keywords:
  - { keyword: "数据分析", weight: 1.5 }

default_skill: false
---

## instructions.general
Worker级数据分析指令
"""


# --- Keyword Matching Tests ---

class TestKeywordWeightedMatching:
    """Test keyword-based skill matching with weighted scores."""

    def test_keyword_weighted_matching(self):
        """Higher weight keywords produce higher scores."""
        analysis_skill = _make_skill(
            "data-analysis",
            keywords=(
                SkillKeyword("数据分析", 1.0),
                SkillKeyword("销售趋势", 0.8),
            ),
        )
        general_skill = _make_skill(
            "general-query",
            keywords=(SkillKeyword("帮我", 0.3),),
            default_skill=True,
        )

        registry = SkillRegistry.from_skills([analysis_skill, general_skill])
        matcher = SkillMatcher(registry)

        result = matcher.match_by_keyword("请帮我做数据分析")
        assert result.status == MatchStatus.KEYWORD_MATCH
        assert result.skill is not None
        assert result.skill.skill_id == "data-analysis"
        # Score should be 1.0 (数据分析)
        assert result.score >= 1.0

    def test_multiple_keyword_hits_sum_weights(self):
        """Multiple keyword matches sum their weights."""
        analysis_skill = _make_skill(
            "data-analysis",
            keywords=(
                SkillKeyword("数据分析", 1.0),
                SkillKeyword("销售趋势", 0.8),
            ),
        )
        registry = SkillRegistry.from_skills([analysis_skill])
        matcher = SkillMatcher(registry)

        result = matcher.match_by_keyword("数据分析销售趋势报告")
        assert result.status == MatchStatus.KEYWORD_MATCH
        assert result.score == pytest.approx(1.8)

    def test_no_keyword_match_returns_not_found(self):
        """No matching keywords returns NOT_FOUND."""
        skill = _make_skill(
            "data-analysis",
            keywords=(SkillKeyword("数据分析", 1.0),),
        )
        registry = SkillRegistry.from_skills([skill])
        matcher = SkillMatcher(registry)

        result = matcher.match_by_keyword("完全无关的话题")
        assert result.status == MatchStatus.NOT_FOUND

    def test_empty_description_returns_not_found(self):
        """Empty task description returns NOT_FOUND."""
        skill = _make_skill("x", keywords=(SkillKeyword("a", 1.0),))
        registry = SkillRegistry.from_skills([skill])
        matcher = SkillMatcher(registry)

        result = matcher.match_by_keyword("  ")
        assert result.status == MatchStatus.NOT_FOUND

    def test_description_fallback_without_keywords(self):
        """Description overlap is used when a skill has no keywords."""
        skill = _make_skill(
            "data-analysis",
            description="Analyze data trends and reporting metrics",
        )
        registry = SkillRegistry.from_skills([skill])
        matcher = SkillMatcher(registry)

        result = matcher.match_by_keyword("please analyze data trends")
        assert result.status == MatchStatus.KEYWORD_MATCH
        assert result.skill is not None
        assert result.skill.skill_id == "data-analysis"
        assert result.score > 0.0

    def test_generic_description_does_not_false_match(self):
        """A generic description should not overpower a specific task."""
        generic = _make_skill(
            "general",
            description="A general purpose assistant for many tasks",
        )
        registry = SkillRegistry.from_skills([generic])
        matcher = SkillMatcher(registry)

        result = matcher.match_by_keyword("分析销售数据并输出趋势")
        assert result.status == MatchStatus.NOT_FOUND

    def test_priority_breaks_score_ties(self):
        """When scores are equal, higher priority wins."""
        skill_a = _make_skill(
            "skill-a",
            priority=5,
            keywords=(SkillKeyword("测试", 1.0),),
        )
        skill_b = _make_skill(
            "skill-b",
            priority=10,
            keywords=(SkillKeyword("测试", 1.0),),
        )
        registry = SkillRegistry.from_skills([skill_a, skill_b])
        matcher = SkillMatcher(registry)

        result = matcher.match_by_keyword("测试任务")
        assert result.skill.skill_id == "skill-b"

    def test_preferred_skill_gets_soft_rerank_bonus(self):
        """Preferred skills win tie-like cases without excluding other matches."""
        skill_a = _make_skill(
            "skill-a",
            keywords=(SkillKeyword("审批", 1.0),),
        )
        skill_b = _make_skill(
            "skill-b",
            keywords=(SkillKeyword("审批", 1.0),),
        )
        registry = SkillRegistry.from_skills([skill_a, skill_b])
        matcher = SkillMatcher(registry)

        result = matcher.match_by_keyword(
            "处理审批请求",
            preferred_skill_ids=("skill-b",),
        )

        assert result.status == MatchStatus.KEYWORD_MATCH
        assert result.skill is not None
        assert result.skill.skill_id == "skill-b"

    def test_stronger_non_preferred_match_can_still_win(self):
        """Soft preference should not block a materially better non-preferred match."""
        strong = _make_skill(
            "strong-skill",
            keywords=(
                SkillKeyword("审批", 1.0),
                SkillKeyword("合同", 1.0),
            ),
        )
        preferred = _make_skill(
            "preferred-skill",
            keywords=(SkillKeyword("审批", 1.0),),
        )
        registry = SkillRegistry.from_skills([strong, preferred])
        matcher = SkillMatcher(registry)

        result = matcher.match_by_keyword(
            "审批合同",
            preferred_skill_ids=("preferred-skill",),
        )

        assert result.status == MatchStatus.KEYWORD_MATCH
        assert result.skill is not None
        assert result.skill.skill_id == "strong-skill"


# --- LLM Fallback Tests ---

class TestLLMFallback:
    """Test LLM-based classification fallback."""

    @pytest.mark.asyncio
    async def test_llm_fallback_on_no_keyword_match(self):
        """LLM classifier is called when no keyword match."""
        skill = _make_skill(
            "data-analysis",
            keywords=(SkillKeyword("数据分析", 1.0),),
        )
        registry = SkillRegistry.from_skills([skill])

        mock_classifier = AsyncMock()
        mock_classifier.classify.return_value = "data-analysis"

        matcher = SkillMatcher(registry, llm_classifier=mock_classifier)
        result = await matcher.match("完全不同的描述")

        assert result.status == MatchStatus.LLM_MATCH
        assert result.skill.skill_id == "data-analysis"
        mock_classifier.classify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_llm_fallback_returns_none(self):
        """When LLM returns None, falls through to default."""
        skill = _make_skill(
            "data-analysis",
            keywords=(SkillKeyword("数据分析", 1.0),),
        )
        default_skill = _make_skill(
            "general-query",
            default_skill=True,
        )
        registry = SkillRegistry.from_skills([skill, default_skill])

        mock_classifier = AsyncMock()
        mock_classifier.classify.return_value = None

        matcher = SkillMatcher(registry, llm_classifier=mock_classifier)
        result = await matcher.match("完全不同的描述")

        assert result.status == MatchStatus.DEFAULT_FALLBACK
        assert result.skill.skill_id == "general-query"

    @pytest.mark.asyncio
    async def test_preferred_skill_fallback_before_default(self):
        preferred = _make_skill("preferred-skill")
        default = _make_skill("default-skill", default_skill=True)
        registry = SkillRegistry.from_skills([preferred, default])
        matcher = SkillMatcher(registry)

        result = await matcher.match(
            "完全无关的描述",
            preferred_skill_ids=("preferred-skill",),
        )

        assert result.status == MatchStatus.DEFAULT_FALLBACK
        assert result.skill is not None
        assert result.skill.skill_id == "preferred-skill"

    @pytest.mark.asyncio
    async def test_llm_classifier_exception_handled(self):
        """LLM classifier exceptions are caught and fallback continues."""
        default_skill = _make_skill("fallback", default_skill=True)
        registry = SkillRegistry.from_skills([default_skill])

        mock_classifier = AsyncMock()
        mock_classifier.classify.side_effect = RuntimeError("LLM error")

        matcher = SkillMatcher(registry, llm_classifier=mock_classifier)
        result = await matcher.match("some query")

        assert result.status == MatchStatus.DEFAULT_FALLBACK


# --- Default Skill Fallback Tests ---

class TestDefaultSkillFallback:
    """Test fallback to default_skill."""

    @pytest.mark.asyncio
    async def test_fallback_to_default_skill_on_no_match(self):
        """Falls back to default_skill when nothing else matches."""
        default = _make_skill("general-query", default_skill=True)
        other = _make_skill(
            "specific",
            keywords=(SkillKeyword("特定", 1.0),),
        )
        registry = SkillRegistry.from_skills([other, default])
        matcher = SkillMatcher(registry)

        result = await matcher.match("无关的请求")
        assert result.status == MatchStatus.DEFAULT_FALLBACK
        assert result.skill.skill_id == "general-query"

    @pytest.mark.asyncio
    async def test_not_found_when_no_default(self):
        """Returns NOT_FOUND when no default and no match."""
        skill = _make_skill(
            "specific",
            keywords=(SkillKeyword("特定", 1.0),),
        )
        registry = SkillRegistry.from_skills([skill])
        matcher = SkillMatcher(registry)

        result = await matcher.match("无关的请求")
        assert result.status == MatchStatus.NOT_FOUND
        assert result.skill is None


# --- Three-Level Override Tests ---

class TestThreeLevelOverride:
    """Test Worker > Tenant > System override semantics."""

    def test_worker_skill_overrides_tenant_skill(self):
        """Worker scope skill overrides tenant scope with same skill_id."""
        system_skill = _make_skill(
            "data-analysis", scope=SkillScope.SYSTEM, priority=10,
        )
        tenant_skill = _make_skill(
            "data-analysis", name="tenant-ver",
            scope=SkillScope.TENANT, priority=10,
        )
        worker_skill = _make_skill(
            "data-analysis", name="worker-ver",
            scope=SkillScope.WORKER, priority=10,
        )

        registry = SkillRegistry.merge(
            system_skills=[system_skill],
            tenant_skills=[tenant_skill],
            worker_skills=[worker_skill],
        )

        result = registry.get("data-analysis")
        assert result is not None
        assert result.scope == SkillScope.WORKER
        assert result.name == "worker-ver"

    def test_tenant_overrides_system(self):
        """Tenant scope overrides system scope."""
        system_skill = _make_skill(
            "data-analysis", scope=SkillScope.SYSTEM,
        )
        tenant_skill = _make_skill(
            "data-analysis", name="tenant-ver",
            scope=SkillScope.TENANT,
        )

        registry = SkillRegistry.merge(
            system_skills=[system_skill],
            tenant_skills=[tenant_skill],
        )

        result = registry.get("data-analysis")
        assert result.scope == SkillScope.TENANT

    def test_system_skills_inherited_when_no_override(self):
        """System skills are available when tenant/worker don't override."""
        system_skill = _make_skill("system-only", scope=SkillScope.SYSTEM)
        tenant_skill = _make_skill("tenant-only", scope=SkillScope.TENANT)

        registry = SkillRegistry.merge(
            system_skills=[system_skill],
            tenant_skills=[tenant_skill],
        )

        assert registry.get("system-only") is not None
        assert registry.get("tenant-only") is not None
        assert len(registry) == 2

    def test_priority_tiebreak_within_same_scope(self):
        """Within same scope, higher priority wins."""
        low_pri = _make_skill(
            "shared-id", name="low", scope=SkillScope.SYSTEM, priority=1,
        )
        high_pri = _make_skill(
            "shared-id", name="high", scope=SkillScope.SYSTEM, priority=10,
        )

        registry = SkillRegistry.from_skills([low_pri, high_pri])
        result = registry.get("shared-id")
        assert result.name == "high"


# --- Directory Scanning Tests ---

class TestMultiLevelDirectoryScan:
    """Test recursive directory scanning."""

    def test_multi_level_directory_scan(self):
        """SKILL.md files in nested subdirectories are discovered."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            # Top-level skill
            _write_skill_md(root, "top-skill", _skill_md("top-skill"))

            # Nested skill
            nested = root / "sub" / "dir"
            _write_skill_md(nested, "nested-skill", _skill_md("nested-skill"))

            loader = SkillLoader()
            skills = loader.scan(root)

            ids = {s.skill_id for s in skills}
            assert "top-skill" in ids
            assert "nested-skill" in ids

    def test_empty_directory_returns_empty(self):
        """Scanning an empty directory returns no skills."""
        with tempfile.TemporaryDirectory() as tmpdir:
            loader = SkillLoader()
            skills = loader.scan(Path(tmpdir))
            assert len(skills) == 0

    def test_nonexistent_directory_returns_empty(self):
        """Scanning a non-existent directory returns empty tuple."""
        loader = SkillLoader()
        skills = loader.scan(Path("/nonexistent/path"))
        assert len(skills) == 0

    def test_parse_failure_skips_and_continues(self):
        """A broken SKILL.md is skipped; other skills still load."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            # Good skill
            _write_skill_md(root, "good-skill", _skill_md("good-skill"))

            # Broken skill (no frontmatter)
            _write_skill_md(root, "broken-skill", "This is not valid SKILL.md")

            loader = SkillLoader()
            skills = loader.scan(root)

            assert len(skills) == 1
            assert skills[0].skill_id == "good-skill"


class TestFullDirectoryScanWithOverride:
    """Test three-level directory scan with registry merge."""

    def test_full_three_level_scan_and_merge(self):
        """System + tenant + worker directories scan and merge correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            system_dir = root / "system" / "skills"
            tenant_dir = root / "tenant" / "skills"
            worker_dir = root / "worker" / "skills"

            _write_skill_md(
                system_dir, "data-analysis",
                _skill_md("data-analysis", scope="system", name="system-ver"),
            )
            _write_skill_md(
                system_dir, "general-query",
                _skill_md("general-query", scope="system", default_skill=True),
            )
            _write_skill_md(
                tenant_dir, "data-analysis",
                _skill_md("data-analysis", scope="tenant", name="tenant-ver"),
            )
            _write_skill_md(
                worker_dir, "data-analysis",
                _skill_md("data-analysis", scope="worker", name="worker-ver"),
            )

            loader = SkillLoader()
            system_skills = loader.scan(system_dir)
            tenant_skills = loader.scan(tenant_dir)
            worker_skills = loader.scan(worker_dir)

            registry = SkillRegistry.merge(
                system_skills=system_skills,
                tenant_skills=tenant_skills,
                worker_skills=worker_skills,
            )

            # Worker version should win for data-analysis
            analysis = registry.get("data-analysis")
            assert analysis.name == "worker-ver"
            assert analysis.scope == SkillScope.WORKER

            # System-only skill should be inherited
            general = registry.get("general-query")
            assert general is not None
            assert general.scope == SkillScope.SYSTEM

    def test_workspace_system_skills_load(self):
        """Real workspace system skills load correctly."""
        workspace_dir = Path(
            "/Users/weiyilan/PycharmProjects/genworker/workspace/system/skills"
        )
        if not workspace_dir.is_dir():
            pytest.skip("Workspace directory not found")

        loader = SkillLoader()
        skills = loader.scan(workspace_dir)

        assert len(skills) >= 2
        ids = {s.skill_id for s in skills}
        assert "data-analysis" in ids
        assert "general-query" in ids

        # Verify data-analysis has hybrid strategy
        analysis = next(s for s in skills if s.skill_id == "data-analysis")
        assert analysis.strategy.mode == StrategyMode.HYBRID
        assert len(analysis.strategy.workflow) == 3


# --- Helpers ---

def _skill_md(
    skill_id: str,
    scope: str = "system",
    name: str = "",
    default_skill: bool = False,
) -> str:
    """Generate a minimal SKILL.md content string."""
    name = name or skill_id
    return f"""\
---
skill_id: "{skill_id}"
name: "{name}"
scope: "{scope}"
priority: 10
strategy:
  mode: "autonomous"
keywords: []
default_skill: {str(default_skill).lower()}
---

## instructions.general
Instructions for {skill_id}
"""
