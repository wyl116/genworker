"""
SubAgent dispatch tool for ReactEngine - enables Worker-as-Coordinator pattern.

Aligns with Claude Code's Coordinator model: the Worker's ReAct loop (LLM)
decides when to spawn SubAgents, reads results as tool_result, and naturally
synthesizes before deciding next steps. No coded decision tree.

The tool wraps SubAgentExecutor.spawn_parallel() + collect_all() and formats
results into an LLM-readable structure returned as a single tool_result.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import uuid4

from src.tools.formatters import ToolResult
from src.tools.mcp.tool import Tool
from src.tools.mcp.types import MCPCategory, RiskLevel, ToolType
from src.worker.planning.models import SubGoal
from src.worker.planning.subagent.aggregator import (
    aggregate_results,
    topological_sort_to_layers,
)
from src.worker.planning.subagent.executor import SubAgentExecutor
from src.worker.planning.subagent.models import (
    AggregatedResult,
    SubAgentContext,
    SubAgentResult,
)

logger = logging.getLogger(__name__)

MAX_SUBTASKS = 5
DEFAULT_TIMEOUT = 120
DEFAULT_MAX_ROUNDS = 10


@dataclass(frozen=True)
class SubTaskSpec:
    """A single sub-task specification from LLM input."""
    id: str
    description: str
    depends_on: tuple[str, ...] = ()
    skill_hint: str | None = None
    preferred_skill_ids: tuple[str, ...] = ()


def _parse_subtasks(raw_tasks: list[dict[str, Any]]) -> tuple[SubTaskSpec, ...]:
    """Parse raw LLM input into validated SubTaskSpec tuple."""
    specs: list[SubTaskSpec] = []
    for i, item in enumerate(raw_tasks[:MAX_SUBTASKS]):
        task_id = item.get("id", f"subtask-{i}")
        description = item.get("description", "")
        if not description:
            continue
        depends_on = tuple(item.get("depends_on", []))
        skill_hint = item.get("skill_hint")
        preferred_skill_ids = _parse_preferred_skill_ids(item)
        specs.append(SubTaskSpec(
            id=task_id,
            description=description,
            depends_on=depends_on,
            skill_hint=skill_hint,
            preferred_skill_ids=preferred_skill_ids,
        ))
    return tuple(specs)


def _specs_to_subgoals(specs: tuple[SubTaskSpec, ...]) -> tuple[SubGoal, ...]:
    """Convert SubTaskSpecs to SubGoal frozen dataclasses."""
    return tuple(
        SubGoal(
            id=spec.id,
            description=spec.description,
            skill_hint=spec.skill_hint,
            preferred_skill_ids=spec.preferred_skill_ids,
            depends_on=spec.depends_on,
        )
        for spec in specs
    )


def _build_contexts(
    sub_goals: tuple[SubGoal, ...],
    worker_id: str,
    parent_task_id: str,
    tool_sandbox: tuple[str, ...],
    timeout: int,
    max_rounds: int,
) -> tuple[SubAgentContext, ...]:
    """Build SubAgentContext for each sub-goal."""
    return tuple(
        SubAgentContext(
            agent_id=f"sa-{worker_id}-{sg.id}",
            parent_worker_id=worker_id,
            parent_task_id=parent_task_id,
            sub_goal=sg,
            skill_id=None,
            preferred_skill_ids=sg.soft_preferred_skill_ids,
            tool_sandbox=tool_sandbox,
            max_rounds=max_rounds,
            timeout_seconds=timeout,
        )
        for sg in sub_goals
    )


def _parse_preferred_skill_ids(item: Mapping[str, Any]) -> tuple[str, ...]:
    """Parse soft-preferred skills from one subtask input."""
    raw = item.get("preferred_skill_ids")
    if raw is None:
        raw = item.get("skills", ())
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    return tuple(
        str(skill_id).strip()
        for skill_id in raw
        if str(skill_id).strip()
    )


def format_results_for_llm(aggregated: AggregatedResult) -> str:
    """
    Format AggregatedResult into LLM-readable structured text.

    Returns a clear summary that enables the LLM to synthesize findings
    and make informed decisions about next steps.
    """
    parts: list[str] = []

    parts.append(
        f"## SubAgent Execution Results\n"
        f"- Completed: {aggregated.success_count}\n"
        f"- Failed: {aggregated.failure_count}\n"
        f"- Total: {len(aggregated.sub_results)}"
    )

    for result in aggregated.sub_results:
        status_icon = "OK" if result.status == "success" else "FAIL"
        header = f"\n### [{status_icon}] {result.sub_goal_id}"
        parts.append(header)

        if result.status == "success":
            content = result.content.strip() if result.content else "(no output)"
            parts.append(content)
        else:
            error_msg = result.error or "Unknown error"
            parts.append(f"Error: {error_msg}")
            if result.content:
                parts.append(f"Partial output: {result.content[:500]}")

    return "\n".join(parts)


async def execute_spawn_subagents(
    tool_input: dict[str, Any],
    executor: SubAgentExecutor,
    worker_id: str,
    parent_task_id: str,
    tool_sandbox: tuple[str, ...],
) -> ToolResult:
    """
    Execute the spawn_subagents tool call.

    Parses LLM input, builds SubAgent contexts, dispatches in parallel,
    collects results, and returns formatted text for LLM synthesis.

    Args:
        tool_input: LLM-provided parameters (subtasks list, strategy, etc.)
        executor: SubAgentExecutor for parallel dispatch.
        worker_id: Parent Worker ID for identity inheritance.
        parent_task_id: Current task ID for lineage tracking.
        tool_sandbox: Available tool names for SubAgents.

    Returns:
        ToolResult with formatted results for LLM consumption.
    """
    raw_tasks = tool_input.get("subtasks", [])
    if not raw_tasks:
        return ToolResult(
            content="Error: 'subtasks' parameter is required and must be non-empty.",
            is_error=True,
        )

    strategy = tool_input.get("strategy", "best_effort")
    timeout = tool_input.get("timeout", DEFAULT_TIMEOUT)
    max_rounds = tool_input.get("max_rounds", DEFAULT_MAX_ROUNDS)

    # Parse and validate
    specs = _parse_subtasks(raw_tasks)
    if not specs:
        return ToolResult(
            content="Error: No valid subtasks found. Each subtask needs a 'description'.",
            is_error=True,
        )

    sub_goals = _specs_to_subgoals(specs)
    try:
        layers = topological_sort_to_layers(sub_goals)
    except Exception as exc:
        return ToolResult(
            content=f"Error: Invalid subtask dependency graph: {exc}",
            is_error=True,
        )

    logger.info(
        f"[SubAgentTool] Spawning {len(sub_goals)} SubAgents for worker '{worker_id}', "
        f"strategy='{strategy}'"
    )

    goal_map = {goal.id: goal for goal in sub_goals}
    collected_results: list[SubAgentResult] = []
    for layer_ids in layers:
        layer_goals = tuple(
            goal_map[layer_id]
            for layer_id in layer_ids
            if layer_id in goal_map
        )
        if not layer_goals:
            continue
        contexts = _build_contexts(
            sub_goals=layer_goals,
            worker_id=worker_id,
            parent_task_id=parent_task_id,
            tool_sandbox=tool_sandbox,
            timeout=timeout,
            max_rounds=max_rounds,
        )
        handles = await executor.spawn_parallel(contexts)
        layer_result = await executor.collect_all(handles, strategy=strategy)
        collected_results.extend(layer_result.sub_results)
        if strategy == "fail_fast" and layer_result.failure_count > 0:
            break
    aggregated = aggregate_results(tuple(collected_results))

    # Format for LLM synthesis
    formatted = format_results_for_llm(aggregated)
    return ToolResult(content=formatted)


# ---------------------------------------------------------------------------
# Tool definition for MCP registration
# ---------------------------------------------------------------------------

SPAWN_SUBAGENTS_DEFINITION: dict[str, Any] = {
    "name": "spawn_subagents",
    "description": (
        "Spawn multiple SubAgents to execute sub-tasks in parallel. "
        "Each subtask runs independently with its own context. "
        "Results are collected and returned for you to synthesize. "
        "You MUST understand the results before taking further action."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "subtasks": {
                "type": "array",
                "description": "List of sub-tasks to execute in parallel",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Unique ID for this subtask",
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "Self-contained task description. SubAgents cannot "
                                "see your conversation history - include all necessary "
                                "context in this description."
                            ),
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "IDs of subtasks this depends on",
                        },
                        "skill_hint": {
                            "type": "string",
                            "description": "Optional skill hint for routing",
                        },
                        "preferred_skill_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional soft-preferred skills for routing",
                        },
                        "skills": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Alias of preferred_skill_ids",
                        },
                    },
                    "required": ["id", "description"],
                },
                "maxItems": MAX_SUBTASKS,
            },
            "strategy": {
                "type": "string",
                "enum": ["best_effort", "fail_fast", "retry_once"],
                "description": (
                    "Collection strategy: best_effort (wait all), "
                    "fail_fast (cancel on first failure), "
                    "retry_once (retry failures once)"
                ),
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout per subtask in seconds",
            },
        },
        "required": ["subtasks"],
    },
    "risk_level": "normal",
}


def create_spawn_subagents_tool(
    executor: SubAgentExecutor,
    worker_id: str,
    parent_task_id: str,
    tool_sandbox: tuple[str, ...],
) -> Tool:
    """
    Create a spawn_subagents Tool instance wired to a SubAgentExecutor.

    The returned Tool can be registered with ToolPipeline/ScopedToolExecutor
    and exposed to the ReactEngine.

    Args:
        executor: SubAgentExecutor for parallel dispatch.
        worker_id: Parent Worker ID.
        parent_task_id: Current task ID.
        tool_sandbox: Available tool names for SubAgents.

    Returns:
        Frozen Tool instance with bound handler.
    """
    async def handler(**kwargs: Any) -> ToolResult:
        return await execute_spawn_subagents(
            tool_input=kwargs,
            executor=executor,
            worker_id=worker_id,
            parent_task_id=parent_task_id,
            tool_sandbox=tool_sandbox,
        )

    return Tool(
        name="spawn_subagents",
        description=SPAWN_SUBAGENTS_DEFINITION["description"],
        handler=handler,
        parameters=SPAWN_SUBAGENTS_DEFINITION["input_schema"],
        required_params=("subtasks",),
        tool_type=ToolType.EXECUTE,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.MEDIUM,
        tags=frozenset({"subagent", "coordination", "parallel"}),
    )
