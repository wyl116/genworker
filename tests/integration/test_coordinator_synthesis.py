# edition: baseline
"""
Integration tests for Worker-as-Coordinator pattern.

Tests the full flow: context_builder injects synthesis instructions,
SubAgent tool is available in tool set, and results are LLM-readable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from src.common.tenant import Tenant, TenantToolPolicy
from src.engine.tools.subagent_tool import (
    create_spawn_subagents_tool,
    format_results_for_llm,
)
from src.tools.mcp.tool import Tool
from src.tools.mcp.types import MCPCategory, RiskLevel, ToolType
from src.worker.context_builder import (
    SYNTHESIS_INSTRUCTIONS,
    build_worker_context,
)
from src.worker.models import Worker, WorkerIdentity, WorkerPersonality, WorkerToolPolicy
from src.worker.planning.subagent.executor import SubAgentExecutor
from src.worker.planning.subagent.models import (
    AggregatedResult,
    SubAgentContext,
    SubAgentResult,
)
from src.worker.trust_gate import WorkerTrustGate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_worker(worker_id: str = "analyst-01") -> Worker:
    return Worker(
        identity=WorkerIdentity(
            name="Analyst",
            worker_id=worker_id,
            version="1.0",
            role="Data Analyst",
            personality=WorkerPersonality(),
        ),
        tool_policy=WorkerToolPolicy(),
        constraints=(),
        triggers=(),
        body_instructions="Analyze data thoroughly.",
        source_path=f"workspace/workers/{worker_id}/PERSONA.md",
    )


def _make_tenant(tenant_id: str = "demo") -> Tenant:
    return Tenant(
        tenant_id=tenant_id,
        name="Demo Tenant",
        trust_level="FULL",
        tool_policy=TenantToolPolicy(),
    )


def _make_trust_gate(trusted: bool = True) -> WorkerTrustGate:
    return WorkerTrustGate(
        trusted=trusted,
        bash_enabled=True,
        mcp_remote_enabled=True,
        learned_rules_enabled=True,
        episodic_write_enabled=True,
    )


def _make_dummy_tool(name: str = "read_file") -> Tool:
    return Tool(
        name=name,
        description=f"Test tool {name}",
        handler=lambda: None,
    )


# ---------------------------------------------------------------------------
# Tests: Synthesis instructions injection
# ---------------------------------------------------------------------------

class TestSynthesisInstructionsInjection:
    def test_injected_when_subagent_enabled(self):
        worker = _make_worker()
        tenant = _make_tenant()
        trust_gate = _make_trust_gate(trusted=True)

        ctx = build_worker_context(
            worker=worker,
            tenant=tenant,
            trust_gate=trust_gate,
            available_tools=(_make_dummy_tool(),),
            subagent_enabled=True,
        )

        assert "spawn_subagents" in ctx.directives
        assert "Synthesize before acting" in ctx.directives
        assert "Never delegate understanding" in ctx.directives

    def test_not_injected_when_subagent_disabled(self):
        worker = _make_worker()
        tenant = _make_tenant()
        trust_gate = _make_trust_gate(trusted=True)

        ctx = build_worker_context(
            worker=worker,
            tenant=tenant,
            trust_gate=trust_gate,
            available_tools=(_make_dummy_tool(),),
            subagent_enabled=False,
        )

        assert "spawn_subagents" not in ctx.directives

    def test_appended_to_existing_directives(self):
        worker = _make_worker()
        tenant = _make_tenant()
        trust_gate = _make_trust_gate(trusted=True)

        ctx = build_worker_context(
            worker=worker,
            tenant=tenant,
            trust_gate=trust_gate,
            available_tools=(_make_dummy_tool(),),
            directives="Always be thorough.",
            subagent_enabled=True,
        )

        assert "Always be thorough." in ctx.directives
        assert "spawn_subagents" in ctx.directives


# ---------------------------------------------------------------------------
# Tests: SubAgent tool in tool set
# ---------------------------------------------------------------------------

class MockTaskExecutor:
    def __init__(self, results: dict[str, str] | None = None) -> None:
        self._results = results or {}

    async def execute_subagent(self, context: SubAgentContext) -> str:
        return self._results.get(context.agent_id, "mock-result")


class TestSubAgentToolInToolSet:
    def test_tool_has_correct_openai_schema(self):
        executor = SubAgentExecutor(
            task_executor=MockTaskExecutor(),
        )
        tool = create_spawn_subagents_tool(
            executor=executor,
            worker_id="analyst-01",
            parent_task_id="task-1",
            tool_sandbox=("read_file", "bash_tool"),
        )

        schema = tool.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "spawn_subagents"
        props = schema["function"]["parameters"]["properties"]
        assert "subtasks" in props
        assert props["subtasks"]["type"] == "array"
        assert "required" in schema["function"]["parameters"]

    @pytest.mark.asyncio
    async def test_end_to_end_tool_execution(self):
        """Simulate full flow: tool creation → execution → LLM-readable result."""
        mock_impl = MockTaskExecutor(results={
            "sa-analyst-01-research": "Found 5 relevant API endpoints in module X.",
            "sa-analyst-01-analysis": "Performance bottleneck in query Y.",
        })
        executor = SubAgentExecutor(
            task_executor=mock_impl,
            max_concurrent_subagents=3,
        )

        tool = create_spawn_subagents_tool(
            executor=executor,
            worker_id="analyst-01",
            parent_task_id="Analyze system performance",
            tool_sandbox=("read_file", "bash_tool"),
        )

        result = await tool.handler(
            subtasks=[
                {
                    "id": "research",
                    "description": "Find all API endpoints in module X",
                },
                {
                    "id": "analysis",
                    "description": "Identify performance bottlenecks in query Y",
                },
            ],
            strategy="best_effort",
        )

        assert not result.is_error
        assert "Completed: 2" in result.content
        assert "5 relevant API endpoints" in result.content
        assert "Performance bottleneck" in result.content

    @pytest.mark.asyncio
    async def test_result_contains_enough_for_synthesis(self):
        """Verify results include enough detail for LLM to synthesize."""
        mock_impl = MockTaskExecutor(results={
            "sa-w1-t1": "Key finding: latency spike at 14:00 UTC correlates with batch job.",
        })
        executor = SubAgentExecutor(
            task_executor=mock_impl,
            max_concurrent_subagents=3,
        )

        tool = create_spawn_subagents_tool(
            executor=executor,
            worker_id="w1",
            parent_task_id="task-1",
            tool_sandbox=(),
        )

        result = await tool.handler(
            subtasks=[{"id": "t1", "description": "Investigate latency"}],
        )

        # The result should contain the SPECIFIC finding, not just a summary
        assert "latency spike at 14:00 UTC" in result.content
        assert "batch job" in result.content
