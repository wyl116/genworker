"""
EnhancedPlanningExecutor - orchestrates the full planning loop.

Flow: decompose -> strategy select -> topological layer execution -> reflect
-> iterate if incomplete (up to max_iterations).
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from dataclasses import replace
from typing import Any, Awaitable, Callable, Protocol

from src.engine.state import WorkerContext

from .decomposer import Decomposer
from .models import PlanningError, PlanningResult, ReflectionResult, SubGoal
from .reflector import Reflector
from .strategy_selector import StrategySelector, StrategyDecision
from .subagent.aggregator import aggregate_results, topological_sort_to_layers
from .subagent.executor import SubAgentExecutor
from .subagent.models import (
    AggregatedResult,
    SubAgentContext,
    SubAgentResult,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS = 3
ProgressCallback = Callable[["PlanningLayerTrace"], Awaitable[None] | None]


class LLMClient(Protocol):
    """Minimal LLM protocol."""

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        intent: object | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class PlanningLayerTrace:
    """Execution trace for one topological layer."""

    iteration: int
    layer_index: int
    sub_goal_ids: tuple[str, ...]
    results: tuple[SubAgentResult, ...]


@dataclass(frozen=True)
class PlanningIterationTrace:
    """Execution trace for one planning iteration."""

    iteration: int
    layer_traces: tuple[PlanningLayerTrace, ...]
    reflection: ReflectionResult
    added_sub_goals: tuple[SubGoal, ...] = ()


@dataclass(frozen=True)
class PlanningExecutionTrace:
    """Structured execution report for the full planning loop."""

    aggregated_result: AggregatedResult
    iterations: tuple[PlanningIterationTrace, ...]
    total_sub_goals: int
    completed_sub_goals: int


class EnhancedPlanningExecutor:
    """
    Orchestrates the full planning loop with decomposition, strategy
    selection, parallel execution, and reflective iteration.

    Wraps the existing execution pipeline with planning capabilities,
    enabled only for open-ended tasks.
    """

    def __init__(
        self,
        decomposer: Decomposer,
        strategy_selector: StrategySelector,
        reflector: Reflector,
        subagent_executor: SubAgentExecutor,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        collection_strategy: str = "best_effort",
    ) -> None:
        self._decomposer = decomposer
        self._selector = strategy_selector
        self._reflector = reflector
        self._subagent_executor = subagent_executor
        self._max_iterations = max_iterations
        self._collection_strategy = collection_strategy

    async def execute(
        self,
        task: str,
        worker_context: WorkerContext,
    ) -> AggregatedResult:
        """Compatibility wrapper returning only the aggregated planning result."""
        trace = await self.execute_with_trace(task, worker_context)
        return trace.aggregated_result

    async def execute_with_trace(
        self,
        task: str,
        worker_context: WorkerContext,
        progress_callback: ProgressCallback | None = None,
    ) -> PlanningExecutionTrace:
        """
        Full planning execution loop.

        Steps:
        1. Decompose task into sub-goals
        2. Select strategy for each sub-goal
        3. Execute sub-goals in topological layers
        4. Reflect on completeness
        5. If incomplete and iterations remain, add sub-goals and repeat from 3

        Returns a structured execution trace with aggregated sub-goal results.
        """
        planning_result = await self._decompose(task, worker_context)
        all_sub_goals = list(planning_result.sub_goals)
        all_results: list[SubAgentResult] = []
        iteration_traces: list[PlanningIterationTrace] = []

        for iteration in range(self._max_iterations):
            iteration_number = iteration + 1
            logger.info(
                f"[EnhancedPlanning] Iteration {iteration_number}/{self._max_iterations}, "
                f"{len(all_sub_goals)} sub-goals"
            )

            # Strategy selection
            current_goals = tuple(
                sg for sg in all_sub_goals if sg.status == "pending"
            )
            if not current_goals:
                break

            decisions = await self._selector.select_batch(
                current_goals,
                candidate_skills=", ".join(
                    worker_context.available_skill_ids
                ) if worker_context.available_skill_ids else "",
            )

            # Execute in topological layers
            layer_results, layer_traces = await self._execute_layers(
                sub_goals=current_goals,
                decisions=decisions,
                worker_context=worker_context,
                parent_task_id=task,
                iteration=iteration_number,
                progress_callback=progress_callback,
            )
            all_results.extend(layer_results)

            # Mark executed goals as completed
            executed_ids = {r.sub_goal_id for r in layer_results}
            all_sub_goals = [
                replace(sg, status="completed") if sg.id in executed_ids else sg
                for sg in all_sub_goals
            ]

            # Reflect
            results_text = _format_results_for_reflection(layer_results)
            reflection = await self._reflect(task, results_text, worker_context)
            added_sub_goals = reflection.additional_sub_goals if (
                self._reflector.needs_iteration(reflection)
                and reflection.additional_sub_goals
            ) else ()
            iteration_traces.append(PlanningIterationTrace(
                iteration=iteration_number,
                layer_traces=layer_traces,
                reflection=reflection,
                added_sub_goals=added_sub_goals,
            ))

            if not self._reflector.needs_iteration(reflection):
                logger.info(
                    f"[EnhancedPlanning] Completeness score "
                    f"{reflection.completeness_score} >= threshold, done."
                )
                break

            # Add additional sub-goals for next iteration
            if added_sub_goals:
                for sg in added_sub_goals:
                    all_sub_goals.append(sg)
                logger.info(
                    f"[EnhancedPlanning] Added {len(added_sub_goals)} "
                    f"additional sub-goals for next iteration"
                )
            else:
                # No additional goals but score is low - stop to avoid infinite loop
                break

        aggregated = aggregate_results(tuple(all_results))
        return PlanningExecutionTrace(
            aggregated_result=aggregated,
            iterations=tuple(iteration_traces),
            total_sub_goals=len(all_sub_goals),
            completed_sub_goals=sum(
                1 for sg in all_sub_goals if sg.status == "completed"
            ),
        )

    async def _decompose(
        self,
        task: str,
        ctx: WorkerContext,
    ) -> PlanningResult:
        """Decompose task into sub-goals via LLM."""
        return await self._decomposer.decompose(
            task=task,
            worker_name=ctx.identity.split("\n")[0] if ctx.identity else "",
            worker_role=ctx.identity,
            available_skills=", ".join(ctx.available_skill_ids) if ctx.available_skill_ids else "",
            episodic_context=ctx.historical_context,
            rules_context=ctx.learned_rules,
        )

    async def _reflect(
        self,
        task: str,
        results_text: str,
        ctx: WorkerContext,
    ) -> ReflectionResult:
        """Evaluate execution completeness via LLM."""
        return await self._reflector.reflect(
            original_task=task,
            sub_goal_results=results_text,
        )

    async def _execute_layers(
        self,
        sub_goals: tuple[SubGoal, ...],
        decisions: tuple[StrategyDecision, ...],
        worker_context: WorkerContext,
        parent_task_id: str,
        iteration: int,
        progress_callback: ProgressCallback | None = None,
    ) -> tuple[list[SubAgentResult], tuple[PlanningLayerTrace, ...]]:
        """Execute sub-goals in topological layers with parallel execution."""
        layers = topological_sort_to_layers(sub_goals)
        decision_map = {d.sub_goal_id: d for d in decisions}
        goal_map = {sg.id: sg for sg in sub_goals}
        all_results: list[SubAgentResult] = []
        layer_traces: list[PlanningLayerTrace] = []

        for layer_index, layer_ids in enumerate(layers, start=1):
            contexts = tuple(
                _build_subagent_context(
                    goal_map[sg_id],
                    decision_map.get(sg_id),
                    worker_context,
                    parent_task_id,
                )
                for sg_id in layer_ids
                if sg_id in goal_map
            )

            if not contexts:
                continue

            handles = await self._subagent_executor.spawn_parallel(contexts)
            agg = await self._subagent_executor.collect_all(
                handles, strategy=self._collection_strategy,
            )
            all_results.extend(agg.sub_results)
            layer_trace = PlanningLayerTrace(
                iteration=iteration,
                layer_index=layer_index,
                sub_goal_ids=tuple(layer_ids),
                results=agg.sub_results,
            )
            layer_traces.append(layer_trace)
            await _invoke_progress_callback(progress_callback, layer_trace)

        return all_results, tuple(layer_traces)


async def _invoke_progress_callback(
    callback: ProgressCallback | None,
    layer_trace: PlanningLayerTrace,
) -> None:
    """Invoke the optional progress callback with awaitable support."""
    if callback is None:
        return
    maybe_awaitable = callback(layer_trace)
    if inspect.isawaitable(maybe_awaitable):
        await maybe_awaitable


def _build_subagent_context(
    sub_goal: SubGoal,
    decision: StrategyDecision | None,
    worker_context: WorkerContext,
    parent_task_id: str,
) -> SubAgentContext:
    """Build a SubAgentContext from a SubGoal and worker context."""
    preferred_skill_ids = _merge_preferred_skill_ids(
        decision.selected_skill if decision is not None else None,
        sub_goal.soft_preferred_skill_ids,
    )
    return SubAgentContext(
        agent_id=f"sa-{worker_context.worker_id}-{sub_goal.id}",
        parent_worker_id=worker_context.worker_id,
        parent_task_id=parent_task_id,
        sub_goal=sub_goal,
        skill_id=None,
        preferred_skill_ids=preferred_skill_ids,
        delegate_worker_id=(
            decision.delegate_to if decision is not None else None
        ),
        tool_sandbox=worker_context.tool_names,
        pre_script=getattr(worker_context, "goal_default_pre_script", None),
    )


def _merge_preferred_skill_ids(
    selected_skill: str | None,
    existing: tuple[str, ...],
) -> tuple[str, ...]:
    """Merge a selected skill into the front of the soft-preference list."""
    merged: list[str] = []
    seen: set[str] = set()
    for skill_id in ((selected_skill,) if selected_skill else ()) + existing:
        skill_text = str(skill_id or "").strip()
        if not skill_text or skill_text in seen:
            continue
        merged.append(skill_text)
        seen.add(skill_text)
    return tuple(merged)


def _format_results_for_reflection(
    results: list[SubAgentResult],
) -> str:
    """Format SubAgent results as text for the reflection prompt."""
    parts: list[str] = []
    for r in results:
        status_icon = "OK" if r.status == "success" else "FAIL"
        parts.append(
            f"[{status_icon}] {r.sub_goal_id}: {r.content[:200] if r.content else r.error or 'no output'}"
        )
    return "\n".join(parts)
