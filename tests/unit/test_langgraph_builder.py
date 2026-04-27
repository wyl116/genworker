# edition: baseline
from pathlib import Path

import pytest

from src.engine.langgraph.builder import build_graph_bundle
from src.engine.langgraph.checkpointer import LangGraphCheckpointer
from src.engine.langgraph.context import NodeContext
from src.engine.langgraph.models import BudgetTracker, LangGraphInitError
from src.engine.protocols import LLMResponse, ToolResult
from src.engine.state import UsageBudget, WorkerContext
from src.services.llm.intent import LLMCallIntent, Purpose
from src.skills.models import (
    GraphDefinition,
    NodeDefinition,
    NodeKind,
    EdgeDefinition,
    Skill,
    SkillStrategy,
    StrategyMode,
)


class _MockLLM:
    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
        return LLMResponse(content="low_risk")


class _MockTools:
    async def execute(self, tool_name, tool_input):
        return ToolResult(content=f"tool:{tool_name}", metadata={"status": "ok"})


def _ctx(tmp_path: Path) -> NodeContext:
    return NodeContext(
        worker_context=WorkerContext(worker_id="worker-1", tenant_id="tenant-1"),
        tools=_MockTools(),
        llm=_MockLLM(),
        checkpointer=LangGraphCheckpointer(tmp_path),
        instruction_resolver=lambda ref: f"instruction:{ref}",
        intent_resolver=lambda ref: LLMCallIntent(purpose=Purpose.GENERATE),
        budget=UsageBudget(),
        tenant_id="tenant-1",
        worker_id="worker-1",
        skill_id="skill-1",
        thread_id="thread-1",
    )


def test_build_yaml_langgraph_bundle(tmp_path: Path):
    skill = Skill(
        skill_id="yaml-graph",
        name="yaml-graph",
        strategy=SkillStrategy(
            mode=StrategyMode.LANGGRAPH,
            graph=GraphDefinition(
                source="yaml",
                state_schema={"task": "str", "status": "str"},
                entry="fetch",
                nodes=(
                    NodeDefinition(name="fetch", kind=NodeKind.TOOL, tool="lookup"),
                    NodeDefinition(
                        name="gate",
                        kind=NodeKind.CONDITION,
                        route={"ok": "END"},
                    ),
                    NodeDefinition(
                        name="pause",
                        kind=NodeKind.INTERRUPT,
                        prompt_ref="approval",
                    ),
                ),
                edges=(
                    EdgeDefinition(from_node="fetch", to_node="gate"),
                    EdgeDefinition(from_node="gate", to_node="END", cond="ok"),
                ),
                max_steps=7,
            ),
        ),
        instructions={"approval": "approve {task}"},
    )

    bundle = build_graph_bundle(
        skill=skill,
        node_context=_ctx(tmp_path),
        budget_tracker=BudgetTracker(),
    )

    assert bundle.state_whitelist == ("task", "status")
    assert bundle.max_steps == 7
    assert "pause" in bundle.interrupt_nodes
    assert hasattr(bundle.compiled, "astream_events")


def test_build_python_langgraph_rejects_invalid_prefix(tmp_path: Path):
    skill = Skill(
        skill_id="python-graph",
        name="python-graph",
        strategy=SkillStrategy(
            mode=StrategyMode.LANGGRAPH,
            graph=GraphDefinition(
                source="python",
                module="evil.graph",
                factory="build_graph",
            ),
        ),
    )

    with pytest.raises(LangGraphInitError, match="outside allowed prefixes"):
        build_graph_bundle(
            skill=skill,
            node_context=_ctx(tmp_path),
            budget_tracker=BudgetTracker(),
        )
