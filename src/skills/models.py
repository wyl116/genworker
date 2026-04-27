"""
Skill data models - all frozen dataclasses for immutability.

Defines:
- Skill: top-level skill definition parsed from SKILL.md
- SkillStrategy: execution strategy (autonomous/deterministic/hybrid)
- WorkflowStep: single step within a hybrid workflow
- SkillKeyword: weighted keyword for intent matching
- RetryConfig: retry configuration for workflow steps
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence


class StrategyMode(Enum):
    """Execution strategy mode."""
    AUTONOMOUS = "autonomous"
    DETERMINISTIC = "deterministic"
    HYBRID = "hybrid"
    PLANNING = "planning"
    LANGGRAPH = "langgraph"


class NodeKind(Enum):
    """Supported LangGraph node kinds for declarative graphs."""
    TOOL = "tool"
    LLM = "llm"
    CONDITION = "condition"
    INTERRUPT = "interrupt"
    PYTHON = "python"


class WorkflowStepType(Enum):
    """Type of a workflow step."""
    AUTONOMOUS = "autonomous"
    DETERMINISTIC = "deterministic"


class SkillScope(Enum):
    """Scope level for three-level override."""
    SYSTEM = "system"
    TENANT = "tenant"
    WORKER = "worker"


# Scope priority: higher value wins in override
SCOPE_PRIORITY: Mapping[SkillScope, int] = {
    SkillScope.SYSTEM: 0,
    SkillScope.TENANT: 1,
    SkillScope.WORKER: 2,
}


@dataclass(frozen=True)
class RetryConfig:
    """Retry configuration for a workflow step."""
    max_attempts: int = 1
    backoff: str = "fixed"


@dataclass(frozen=True)
class WorkflowStep:
    """Single step in a hybrid workflow."""
    step: str
    type: WorkflowStepType
    instruction_ref: str = ""
    max_rounds: int = 1
    tools: tuple[str, ...] = ()
    retry: RetryConfig = RetryConfig()


@dataclass(frozen=True)
class SkillKeyword:
    """Weighted keyword for intent matching."""
    keyword: str
    weight: float = 1.0


@dataclass(frozen=True)
class FallbackConfig:
    """Fallback configuration when primary strategy cannot execute."""
    condition: str = ""
    mode: str = "autonomous"


@dataclass(frozen=True)
class NodeDefinition:
    """Declarative graph node definition for langgraph strategy."""
    name: str
    kind: NodeKind
    tool: str = ""
    instruction_ref: str = ""
    tools: tuple[str, ...] = ()
    prompt_ref: str = ""
    inbox_event_type: str = "langgraph.interrupt"
    route: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EdgeDefinition:
    """Directed graph edge definition."""
    from_node: str
    to_node: str
    cond: Optional[str] = None


@dataclass(frozen=True)
class GraphDefinition:
    """Top-level graph definition for langgraph strategy."""
    source: str
    state_schema: Mapping[str, str] = field(default_factory=dict)
    entry: str = ""
    nodes: tuple[NodeDefinition, ...] = ()
    edges: tuple[EdgeDefinition, ...] = ()
    module: str = ""
    factory: str = ""
    state_schema_ref: str = ""
    max_steps: int = 50


@dataclass(frozen=True)
class SkillStrategy:
    """Execution strategy for a skill."""
    mode: StrategyMode = StrategyMode.AUTONOMOUS
    workflow: tuple[WorkflowStep, ...] = ()
    fallback: Optional[FallbackConfig] = None
    graph: Optional[GraphDefinition] = None


@dataclass(frozen=True)
class Skill:
    """
    Complete skill definition parsed from a SKILL.md file.

    Immutable — use dataclasses.replace() to produce modified copies.
    """
    skill_id: str
    name: str
    description: str = ""
    version: str = "1.0"
    scope: SkillScope = SkillScope.SYSTEM
    priority: int = 0
    strategy: SkillStrategy = SkillStrategy()
    keywords: tuple[SkillKeyword, ...] = ()
    recommended_tools: tuple[str, ...] = ()
    gate_level: str = "gated"
    default_skill: bool = False
    instructions: Mapping[str, str] = field(default_factory=dict)
    source_format: str = "genworker_legacy"
    extra_metadata: Mapping[str, Any] = field(default_factory=dict)
    source_path: str = ""

    def get_instruction(self, phase: str) -> str:
        """Get instruction text for a given phase, falling back to general."""
        return self.instructions.get(phase, self.instructions.get("general", ""))
