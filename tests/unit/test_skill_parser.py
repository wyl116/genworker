# edition: baseline
"""
Unit tests for SKILL.md parser.

Tests:
- Hybrid strategy with workflow parsing
- Phased instruction extraction
- Keyword parsing with weights
- Autonomous and deterministic mode parsing
- Error handling for invalid files
"""
import tempfile
from pathlib import Path

import pytest

from src.skills.models import (
    SkillScope,
    StrategyMode,
    WorkflowStepType,
)
from src.skills.parser import SkillParser, parse_skill_file
from src.common.exceptions import SkillException


# --- Fixtures ---

HYBRID_SKILL_MD = """\
---
skill_id: "data-analysis"
name: "数据分析"
version: "1.0"
scope: "system"
priority: 10

strategy:
  mode: "hybrid"
  fallback:
    condition: "no_data_tools_available"
    mode: "autonomous"
  workflow:
    - step: "planning"
      type: "autonomous"
      instruction_ref: "planning"
      max_rounds: 3
    - step: "execution"
      type: "deterministic"
      instruction_ref: "execution"
      tools: ["sql_executor", "data_profiler"]
      retry: { max_attempts: 2, backoff: "exponential" }
    - step: "summarization"
      type: "autonomous"
      instruction_ref: "summarization"

keywords:
  - { keyword: "数据分析", weight: 1.0 }
  - { keyword: "销售趋势", weight: 0.8 }
  - { keyword: "数据质量", weight: 0.7 }

recommended_tools:
  - "sql_executor"
  - "data_profiler"

default_skill: false
---

# 数据分析 Skill

## instructions.general
全量领域知识

## instructions.planning
规划阶段指令

## instructions.execution
执行阶段指令

## instructions.summarization
总结阶段指令
"""

V2_SKILL_MD = """\
---
name: "v2-analysis"
description: "Analyze datasets and trends."
version: "1.1"
metadata:
  genworker:
    scope: "worker"
    priority: 7
    strategy:
      mode: "autonomous"
    keywords:
      - { keyword: "trend", weight: 0.8 }
    recommended_tools:
      - "sql_executor"
    gate_level: "auto"
    default_skill: true
---

## instructions.general
V2 instructions
"""

OPENCLAW_SKILL_MD = """\
---
name: "todoist-cli"
description: "Manage Todoist tasks from the command line."
version: "1.2.0"
metadata:
  openclaw:
    primaryEnv: "TODOIST_API_KEY"
---

Manage Todoist tasks directly.
"""

AUTONOMOUS_SKILL_MD = """\
---
skill_id: "general-query"
name: "通用查询"
version: "1.0"
scope: "tenant"
priority: 5

strategy:
  mode: "autonomous"

keywords:
  - { keyword: "查询", weight: 0.5 }

recommended_tools: []

default_skill: true
---

# 通用查询

## instructions.general
通用查询指令
"""

MINIMAL_SKILL_MD = """\
---
skill_id: "minimal"
---

Some content here.
"""

PLANNING_SKILL_MD = """\
---
skill_id: "complex-research"
name: "复杂研究"

strategy:
  mode: "planning"

keywords:
  - { keyword: "研究", weight: 1.0 }
---

## instructions.general
先拆解任务，再执行。
"""


def _write_temp_skill(content: str) -> Path:
    """Write content to a temporary SKILL.md file and return the path."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8",
    )
    tmp.write(content)
    tmp.flush()
    tmp.close()
    return Path(tmp.name)


# --- Tests ---

class TestParseHybridStrategy:
    """Test parsing hybrid strategy with workflow."""

    def test_parse_hybrid_strategy_with_workflow(self):
        path = _write_temp_skill(HYBRID_SKILL_MD)
        skill = parse_skill_file(path)

        assert skill.skill_id == "data-analysis"
        assert skill.name == "数据分析"
        assert skill.description == ""
        assert skill.version == "1.0"
        assert skill.scope == SkillScope.SYSTEM
        assert skill.priority == 10
        assert skill.source_format == "genworker_legacy"
        assert skill.extra_metadata == {}

        # Strategy
        assert skill.strategy.mode == StrategyMode.HYBRID
        assert len(skill.strategy.workflow) == 3

        # Fallback
        assert skill.strategy.fallback is not None
        assert skill.strategy.fallback.condition == "no_data_tools_available"
        assert skill.strategy.fallback.mode == "autonomous"

    def test_workflow_steps(self):
        path = _write_temp_skill(HYBRID_SKILL_MD)
        skill = parse_skill_file(path)
        workflow = skill.strategy.workflow

        # Planning step
        planning = workflow[0]
        assert planning.step == "planning"
        assert planning.type == WorkflowStepType.AUTONOMOUS
        assert planning.instruction_ref == "planning"
        assert planning.max_rounds == 3

        # Execution step
        execution = workflow[1]
        assert execution.step == "execution"
        assert execution.type == WorkflowStepType.DETERMINISTIC
        assert execution.tools == ("sql_executor", "data_profiler")
        assert execution.retry.max_attempts == 2
        assert execution.retry.backoff == "exponential"

        # Summarization step
        summarization = workflow[2]
        assert summarization.step == "summarization"
        assert summarization.type == WorkflowStepType.AUTONOMOUS
        assert summarization.instruction_ref == "summarization"

    def test_recommended_tools(self):
        path = _write_temp_skill(HYBRID_SKILL_MD)
        skill = parse_skill_file(path)

        assert skill.recommended_tools == ("sql_executor", "data_profiler")

    def test_default_skill_false(self):
        path = _write_temp_skill(HYBRID_SKILL_MD)
        skill = parse_skill_file(path)

        assert skill.default_skill is False


class TestParsePhasedInstructions:
    """Test phased instruction extraction."""

    def test_parse_phased_instructions(self):
        path = _write_temp_skill(HYBRID_SKILL_MD)
        skill = parse_skill_file(path)

        assert "general" in skill.instructions
        assert "planning" in skill.instructions
        assert "execution" in skill.instructions
        assert "summarization" in skill.instructions

        assert skill.instructions["general"] == "全量领域知识"
        assert skill.instructions["planning"] == "规划阶段指令"
        assert skill.instructions["execution"] == "执行阶段指令"
        assert skill.instructions["summarization"] == "总结阶段指令"

    def test_get_instruction_method(self):
        path = _write_temp_skill(HYBRID_SKILL_MD)
        skill = parse_skill_file(path)

        assert skill.get_instruction("planning") == "规划阶段指令"
        assert skill.get_instruction("nonexistent") == "全量领域知识"

    def test_general_only_instruction(self):
        path = _write_temp_skill(AUTONOMOUS_SKILL_MD)
        skill = parse_skill_file(path)

        assert "general" in skill.instructions
        assert skill.instructions["general"] == "通用查询指令"


class TestParseKeywords:
    """Test keyword parsing."""

    def test_keyword_parsing_with_weights(self):
        path = _write_temp_skill(HYBRID_SKILL_MD)
        skill = parse_skill_file(path)

        assert len(skill.keywords) == 3
        assert skill.keywords[0].keyword == "数据分析"
        assert skill.keywords[0].weight == 1.0
        assert skill.keywords[1].keyword == "销售趋势"
        assert skill.keywords[1].weight == 0.8
        assert skill.keywords[2].keyword == "数据质量"
        assert skill.keywords[2].weight == 0.7


class TestParseAutonomousMode:
    """Test autonomous mode parsing."""

    def test_autonomous_strategy(self):
        path = _write_temp_skill(AUTONOMOUS_SKILL_MD)
        skill = parse_skill_file(path)

        assert skill.strategy.mode == StrategyMode.AUTONOMOUS
        assert len(skill.strategy.workflow) == 0
        assert skill.strategy.fallback is None

    def test_tenant_scope(self):
        path = _write_temp_skill(AUTONOMOUS_SKILL_MD)
        skill = parse_skill_file(path)

        assert skill.scope == SkillScope.TENANT

    def test_default_skill_true(self):
        path = _write_temp_skill(AUTONOMOUS_SKILL_MD)
        skill = parse_skill_file(path)

        assert skill.default_skill is True


class TestParsePlanningMode:
    """Test planning mode parsing."""

    def test_planning_strategy(self):
        path = _write_temp_skill(PLANNING_SKILL_MD)
        skill = parse_skill_file(path)

        assert skill.strategy.mode == StrategyMode.PLANNING
        assert len(skill.strategy.workflow) == 0


class TestMinimalSkill:
    """Test minimal skill with only required fields."""

    def test_minimal_skill_parses(self):
        path = _write_temp_skill(MINIMAL_SKILL_MD)
        skill = parse_skill_file(path)

        assert skill.skill_id == "minimal"
        assert skill.name == "minimal"
        assert skill.source_format == "genworker_legacy"
        assert skill.scope == SkillScope.SYSTEM
        assert skill.priority == 0
        assert skill.strategy.mode == StrategyMode.AUTONOMOUS
        assert len(skill.keywords) == 0
        assert len(skill.recommended_tools) == 0
        assert skill.default_skill is False

    def test_minimal_body_goes_to_general(self):
        path = _write_temp_skill(MINIMAL_SKILL_MD)
        skill = parse_skill_file(path)

        assert "general" in skill.instructions
        assert "Some content here." in skill.instructions["general"]


class TestParserErrorHandling:
    """Test parser error handling."""

    def test_missing_frontmatter_raises(self):
        path = _write_temp_skill("No frontmatter here")
        with pytest.raises(SkillException, match="missing YAML frontmatter"):
            parse_skill_file(path)

    def test_missing_name_and_skill_id_raises(self):
        content = """\
---
description: "no identifier"
---

Content
"""
        path = _write_temp_skill(content)
        with pytest.raises(SkillException, match="Missing required 'skill_id' or 'name'"):
            parse_skill_file(path)

    def test_invalid_yaml_raises(self):
        content = """\
---
skill_id: [invalid yaml
  broken: {{
---

Content
"""
        path = _write_temp_skill(content)
        with pytest.raises(SkillException, match="Invalid YAML"):
            parse_skill_file(path)

    def test_nonexistent_file_raises(self):
        with pytest.raises(SkillException, match="Cannot read"):
            parse_skill_file(Path("/nonexistent/SKILL.md"))


class TestSkillParserFacade:
    """Test the SkillParser class facade."""

    def test_parser_facade(self):
        path = _write_temp_skill(HYBRID_SKILL_MD)
        parser = SkillParser()
        skill = parser.parse(path)

        assert skill.skill_id == "data-analysis"


class TestNewFormatParsing:
    """Test genworker v2 and OpenClaw-native parsing."""

    def test_parse_v2_skill(self):
        path = _write_temp_skill(V2_SKILL_MD)
        skill = parse_skill_file(path)

        assert skill.skill_id == "v2-analysis"
        assert skill.name == "v2-analysis"
        assert skill.description == "Analyze datasets and trends."
        assert skill.scope == SkillScope.WORKER
        assert skill.priority == 7
        assert skill.strategy.mode == StrategyMode.AUTONOMOUS
        assert skill.keywords[0].keyword == "trend"
        assert skill.recommended_tools == ("sql_executor",)
        assert skill.gate_level == "auto"
        assert skill.default_skill is True
        assert skill.source_format == "genworker_v2"
        assert skill.extra_metadata == {}

    def test_parse_openclaw_skill(self):
        path = _write_temp_skill(OPENCLAW_SKILL_MD)
        skill = parse_skill_file(path)

        assert skill.skill_id == "todoist-cli"
        assert skill.name == "todoist-cli"
        assert skill.description == "Manage Todoist tasks from the command line."
        assert skill.version == "1.2.0"
        assert skill.scope == SkillScope.SYSTEM
        assert skill.priority == 0
        assert skill.source_format == "openclaw"
        assert skill.extra_metadata == {
            "openclaw": {"primaryEnv": "TODOIST_API_KEY"},
        }
        assert skill.instructions["general"] == "Manage Todoist tasks directly."

    def test_legacy_name_still_preserved(self):
        path = _write_temp_skill(HYBRID_SKILL_MD)
        skill = parse_skill_file(path)

        assert skill.skill_id == "data-analysis"
        assert skill.name == "数据分析"

    def test_mixed_legacy_and_name_prefers_legacy(self):
        path = _write_temp_skill(HYBRID_SKILL_MD)
        skill = parse_skill_file(path)

        assert skill.source_format == "genworker_legacy"
        assert skill.skill_id == "data-analysis"
        assert skill.name == "数据分析"


class TestImmutability:
    """Test that Skill objects are truly frozen."""

    def test_skill_is_frozen(self):
        path = _write_temp_skill(HYBRID_SKILL_MD)
        skill = parse_skill_file(path)

        with pytest.raises(AttributeError):
            skill.name = "modified"

    def test_strategy_is_frozen(self):
        path = _write_temp_skill(HYBRID_SKILL_MD)
        skill = parse_skill_file(path)

        with pytest.raises(AttributeError):
            skill.strategy.mode = StrategyMode.AUTONOMOUS
