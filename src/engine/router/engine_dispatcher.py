"""
EngineDispatcher - routes execution to the correct engine based on Skill strategy.

Supports fallback: when a hybrid/deterministic skill cannot execute,
degrades to autonomous mode if fallback is configured.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, AsyncGenerator
from uuid import uuid4

from src.common.logger import get_logger
from src.context.models import ContextWindowConfig
from src.context.prefix_cache import StablePrefixCache
from src.engine.checkpoint import EngineHandoff, with_engine
from src.engine.hybrid.engine import HybridEngine
from src.engine.prompt_builder import PromptBuilder
from src.engine.protocols import LLMClient, ToolExecutor
from src.engine.react.agent import ReactEngine
from src.engine.state import UsageBudget, WorkerContext
from src.engine.workflow.engine import WorkflowEngine
from src.skills.models import Skill, StrategyMode
from src.streaming.events import StreamEvent
from src.streaming.events import (
    ErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TaskProgressEvent,
    TextMessageEvent,
)

logger = get_logger()


class EngineDispatcher:
    """
    Routes execution to the correct engine based on skill strategy mode.

    Supports fallback degradation to autonomous when the primary
    strategy cannot execute.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        tool_executor: ToolExecutor,
        max_rounds: int = 10,
        mcp_server: Any | None = None,
        memory_flush_callback: Any | None = None,
        enhanced_planning_executor: Any | None = None,
        state_checkpointer: Any | None = None,
        langgraph_engine: Any | None = None,
    ) -> None:
        self._llm = llm_client
        self._tool_executor = tool_executor
        self._max_rounds = max_rounds
        self._mcp_server = mcp_server
        self._memory_flush_callback = memory_flush_callback
        self._enhanced_planning_executor = enhanced_planning_executor
        self._prefix_cache = StablePrefixCache()
        self._state_checkpointer = state_checkpointer
        self.langgraph_engine = langgraph_engine

    async def dispatch(
        self,
        skill: Skill,
        worker_context: WorkerContext,
        task: str,
        available_tools: list[dict[str, Any]] | None = None,
        budget: UsageBudget | None = None,
        run_id: str | None = None,
        context_config: ContextWindowConfig | None = None,
        max_rounds_override: int | None = None,
        checkpoint_handle: Any | None = None,
        resume_from: Any | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """
        Dispatch execution to the appropriate engine.

        Args:
            skill: Skill defining the execution strategy.
            worker_context: Worker context for prompt building.
            task: User task text.
            available_tools: Tool definitions.
            budget: Optional token budget.
            run_id: Optional run identifier.

        Yields:
            StreamEvent frozen dataclasses.
        """
        run_id = run_id or uuid4().hex
        mode = skill.strategy.mode
        worker_context = replace(worker_context, skill_id=skill.skill_id)

        # Check fallback condition
        if self._should_fallback(skill, mode):
            logger.info(
                f"[EngineDispatcher] Skill '{skill.name}' falling back "
                f"from {mode.value} to autonomous"
            )
            mode = StrategyMode.AUTONOMOUS

        if mode == StrategyMode.AUTONOMOUS:
            async for event in self._dispatch_autonomous(
                skill,
                worker_context,
                task,
                available_tools,
                budget,
                run_id,
                max_rounds_override,
                checkpoint_handle,
                resume_from,
            ):
                yield event

        elif mode == StrategyMode.DETERMINISTIC:
            async for event in self._dispatch_deterministic(
                skill,
                worker_context,
                task,
                available_tools,
                budget,
                run_id,
                checkpoint_handle,
                resume_from,
            ):
                yield event

        elif mode == StrategyMode.HYBRID:
            async for event in self._dispatch_hybrid(
                skill,
                worker_context,
                task,
                available_tools,
                budget,
                run_id,
                checkpoint_handle,
                resume_from,
            ):
                yield event

        elif mode == StrategyMode.PLANNING:
            async for event in self._dispatch_planning(
                worker_context=worker_context,
                task=task,
                run_id=run_id,
                checkpoint_handle=checkpoint_handle,
            ):
                yield event

        elif mode == StrategyMode.LANGGRAPH:
            async for event in self._dispatch_langgraph(
                skill=skill,
                worker_context=worker_context,
                task=task,
                available_tools=available_tools,
                budget=budget,
                run_id=run_id,
                checkpoint_handle=checkpoint_handle,
            ):
                yield event

        else:
            logger.error(f"[EngineDispatcher] Unknown strategy mode: {mode}")
            async for event in self._dispatch_autonomous(
                skill,
                worker_context,
                task,
                available_tools,
                budget,
                run_id,
                max_rounds_override,
            ):
                yield event

    def _should_fallback(self, skill: Skill, mode: StrategyMode) -> bool:
        """Check if fallback should be triggered."""
        if mode == StrategyMode.AUTONOMOUS:
            return False

        fallback = skill.strategy.fallback
        if fallback is None:
            return False

        condition = fallback.condition

        if condition == "no_workflow":
            return not skill.strategy.workflow

        if condition == "empty_steps":
            return len(skill.strategy.workflow) == 0

        return False

    async def _dispatch_autonomous(
        self,
        skill: Skill,
        worker_context: WorkerContext,
        task: str,
        available_tools: list[dict[str, Any]] | None,
        budget: UsageBudget | None,
        run_id: str,
        max_rounds_override: int | None,
        checkpoint_handle: Any | None,
        resume_from: Any | None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Dispatch to ReactEngine."""
        engine = ReactEngine(
            llm_client=self._llm,
            tool_executor=self._tool_executor,
            max_rounds=max_rounds_override or self._max_rounds,
            mcp_server=self._mcp_server,
            memory_flush_callback=self._memory_flush_callback,
            prefix_cache=self._prefix_cache,
        )
        system_prompt = PromptBuilder.build_autonomous(worker_context, skill)

        async for event in engine.execute(
            system_prompt=system_prompt,
            task=task,
            tools=available_tools,
            budget=budget,
            run_id=run_id,
            worker_context=worker_context,
            context_config=ContextWindowConfig(),
            checkpoint_handle=with_engine(checkpoint_handle, "react"),
            state_checkpointer=self._state_checkpointer,
            resume_from=resume_from,
        ):
            yield event

    async def _dispatch_deterministic(
        self,
        skill: Skill,
        worker_context: WorkerContext,
        task: str,
        available_tools: list[dict[str, Any]] | None,
        budget: UsageBudget | None,
        run_id: str,
        checkpoint_handle: Any | None,
        resume_from: Any | None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Dispatch to WorkflowEngine."""
        engine = WorkflowEngine(
            llm_client=self._llm,
            tool_executor=self._tool_executor,
        )

        def build_step_prompt(step, previous_input):
            return PromptBuilder.build_deterministic_step(
                worker_context, skill, step, previous_input
            )

        async for event in engine.execute(
            steps=skill.strategy.workflow,
            task=task,
            build_step_prompt=build_step_prompt,
            available_tools=available_tools,
            budget=budget,
            run_id=run_id,
            checkpoint_handle=with_engine(checkpoint_handle, "deterministic"),
            state_checkpointer=self._state_checkpointer,
            resume_from=resume_from,
            worker_context=worker_context,
        ):
            yield event

    async def _dispatch_hybrid(
        self,
        skill: Skill,
        worker_context: WorkerContext,
        task: str,
        available_tools: list[dict[str, Any]] | None,
        budget: UsageBudget | None,
        run_id: str,
        checkpoint_handle: Any | None,
        resume_from: Any | None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Dispatch to HybridEngine."""
        engine = HybridEngine(
            llm_client=self._llm,
            tool_executor=self._tool_executor,
        )

        def build_step_prompt(step, previous_input):
            return PromptBuilder.build_deterministic_step(
                worker_context, skill, step, previous_input
            )

        def build_autonomous_prompt(step):
            return PromptBuilder.build_autonomous(
                worker_context, skill, instruction_override=step.instruction_ref or None
            )

        async for event in engine.execute(
            steps=skill.strategy.workflow,
            task=task,
            build_step_prompt=build_step_prompt,
            build_autonomous_prompt=build_autonomous_prompt,
            available_tools=available_tools,
            budget=budget,
            run_id=run_id,
            checkpoint_handle=with_engine(checkpoint_handle, "hybrid"),
            state_checkpointer=self._state_checkpointer,
            resume_from=resume_from,
            worker_context=worker_context,
        ):
            yield event

    async def _dispatch_planning(
        self,
        *,
        worker_context: WorkerContext,
        task: str,
        run_id: str,
        checkpoint_handle: Any | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Dispatch to EnhancedPlanningExecutor and adapt the result to stream events."""
        yield RunStartedEvent(run_id=run_id)
        yield StepStartedEvent(
            run_id=run_id,
            step_name="planning",
            step_type=StrategyMode.PLANNING.value,
        )

        executor = self._enhanced_planning_executor
        if executor is None:
            message = "EnhancedPlanningExecutor is not configured"
            logger.warning(f"[EngineDispatcher] {message}")
            yield ErrorEvent(
                run_id=run_id,
                code="PLANNING_EXECUTOR_UNAVAILABLE",
                message=message,
            )
            yield StepFinishedEvent(
                run_id=run_id,
                step_name="planning",
                success=False,
            )
            yield RunFinishedEvent(
                run_id=run_id,
                success=False,
                stop_reason=message,
            )
            return

        progress_queue: asyncio.Queue[StreamEvent] = asyncio.Queue()

        async def _enqueue_layer_progress(layer_trace) -> None:
            for event in _build_layer_progress_events(
                run_id=run_id,
                layer_trace=layer_trace,
            ):
                await progress_queue.put(event)

        try:
            execute_with_trace = getattr(executor, "execute_with_trace", None)
            if callable(execute_with_trace):
                trace_task = asyncio.create_task(
                    execute_with_trace(
                        task=task,
                        worker_context=worker_context,
                        progress_callback=_enqueue_layer_progress,
                    )
                )
                while True:
                    if trace_task.done() and progress_queue.empty():
                        break
                    try:
                        event = await asyncio.wait_for(
                            progress_queue.get(), timeout=0.05,
                        )
                    except TimeoutError:
                        continue
                    else:
                        yield event
                trace = await trace_task
                aggregated = trace.aggregated_result
            else:
                aggregated = await executor.execute(
                    task=task,
                    worker_context=worker_context,
                )
        except Exception as exc:
            message = str(exc)
            logger.error(
                "[EngineDispatcher] Planning execution failed: %s",
                exc,
                exc_info=True,
            )
            yield ErrorEvent(
                run_id=run_id,
                code="PLANNING_EXECUTION_ERROR",
                message=message,
            )
            yield StepFinishedEvent(
                run_id=run_id,
                step_name="planning",
                success=False,
            )
            yield RunFinishedEvent(
                run_id=run_id,
                success=False,
                stop_reason=message,
            )
            return

        content = _format_planning_content(aggregated)
        if not callable(getattr(executor, "execute_with_trace", None)):
            for event in _build_planning_progress_events(
                run_id=run_id,
                aggregated=aggregated,
            ):
                yield event
        if content:
            yield TextMessageEvent(run_id=run_id, content=content)

        if self._state_checkpointer is not None and checkpoint_handle is not None:
            from src.engine.checkpoint import ExecutionSnapshot, make_checkpoint_ref
            from src.engine.serializer import serialize_worker_context

            handoff = EngineHandoff(
                source_engine="planning",
                target_engine="react",
                payload={"content": content},
            )
            await self._state_checkpointer.save(
                ExecutionSnapshot(
                    checkpoint_ref=make_checkpoint_ref(
                        with_engine(checkpoint_handle, "planning"),
                        round_number=0,
                        metadata={"kind": "handoff"},
                    ),
                    budget={},
                    worker_context=serialize_worker_context(worker_context),
                    handoff_payload=handoff.payload,
                )
            )

        yield StepFinishedEvent(
            run_id=run_id,
            step_name="planning",
            success=aggregated.failure_count == 0,
        )
        yield RunFinishedEvent(
            run_id=run_id,
            success=True,
            stop_reason="planning_complete",
        )

    async def _dispatch_langgraph(
        self,
        *,
        skill: Skill,
        worker_context: WorkerContext,
        task: str,
        available_tools: list[dict[str, Any]] | None,
        budget: UsageBudget | None,
        run_id: str,
        checkpoint_handle: Any | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Dispatch to LangGraphEngine with local fallback handling."""
        engine = self.langgraph_engine
        if engine is None:
            message = "LangGraphEngine is not configured"
            logger.warning("[EngineDispatcher] %s", message)
            async for event in self._dispatch_langgraph_fallback(
                skill=skill,
                worker_context=worker_context,
                task=task,
                available_tools=available_tools,
                budget=budget,
                run_id=run_id,
                checkpoint_handle=checkpoint_handle,
                message=message,
            ):
                yield event
            return
        try:
            async for event in engine.execute(
                skill,
                worker_context,
                task,
                available_tools=available_tools,
                budget=budget,
                run_id=run_id,
                checkpoint_handle=checkpoint_handle,
            ):
                yield event
        except Exception as exc:
            async for event in self._dispatch_langgraph_fallback(
                skill=skill,
                worker_context=worker_context,
                task=task,
                available_tools=available_tools,
                budget=budget,
                run_id=run_id,
                checkpoint_handle=checkpoint_handle,
                message=str(exc),
            ):
                yield event

    async def _dispatch_langgraph_fallback(
        self,
        *,
        skill: Skill,
        worker_context: WorkerContext,
        task: str,
        available_tools: list[dict[str, Any]] | None,
        budget: UsageBudget | None,
        run_id: str,
        checkpoint_handle: Any | None,
        message: str,
    ) -> AsyncGenerator[StreamEvent, None]:
        fallback = skill.strategy.fallback
        fallback_mode = getattr(getattr(fallback, "mode", None), "value", getattr(fallback, "mode", ""))
        if fallback is not None and str(fallback_mode).lower() == StrategyMode.AUTONOMOUS.value:
            logger.warning(
                "[EngineDispatcher] LangGraph fallback for skill=%s: %s",
                skill.skill_id,
                message,
            )
            async for event in self._dispatch_autonomous(
                skill,
                worker_context,
                task,
                available_tools,
                budget,
                run_id,
                None,
                checkpoint_handle,
                None,
            ):
                yield event
            return
        yield ErrorEvent(
            run_id=run_id,
            code="LANGGRAPH_UNAVAILABLE",
            message=message,
        )
        yield RunFinishedEvent(run_id=run_id, success=False, stop_reason=message)


def _format_planning_content(aggregated: Any) -> str:
    """Normalize AggregatedResult into assistant-facing text output."""
    combined = str(getattr(aggregated, "combined_content", "") or "").strip()
    if combined:
        return combined

    sub_results = getattr(aggregated, "sub_results", ()) or ()
    if not sub_results:
        return ""

    lines: list[str] = []
    for result in sub_results:
        goal_id = getattr(result, "sub_goal_id", "unknown")
        status = getattr(result, "status", "unknown")
        content = str(getattr(result, "content", "") or "").strip()
        error = str(getattr(result, "error", "") or "").strip()
        detail = content or error or "no output"
        lines.append(f"[{status}] {goal_id}: {detail}")
    return "\n".join(lines)


def _build_planning_progress_events(
    *,
    run_id: str,
    aggregated: Any,
) -> tuple[TaskProgressEvent, ...]:
    """Expand sub-goal results into coarse-grained progress events."""
    sub_results = tuple(getattr(aggregated, "sub_results", ()) or ())
    if not sub_results:
        return ()

    total = len(sub_results)
    events: list[TaskProgressEvent] = []
    for index, result in enumerate(sub_results, start=1):
        goal_id = str(getattr(result, "sub_goal_id", "") or f"sub-goal-{index}")
        status = str(getattr(result, "status", "unknown") or "unknown")
        progress = index / total
        events.append(TaskProgressEvent(
            run_id=run_id,
            task_id=goal_id,
            progress=progress,
            current_step=f"{goal_id} ({status})",
        ))
    return tuple(events)


def _build_layer_progress_events(
    *,
    run_id: str,
    layer_trace: Any,
) -> tuple[TaskProgressEvent, ...]:
    """Build progress events for a completed planning layer."""
    results = tuple(getattr(layer_trace, "results", ()) or ())
    if not results:
        return ()

    iteration = int(getattr(layer_trace, "iteration", 0) or 0)
    layer_index = int(getattr(layer_trace, "layer_index", 0) or 0)
    total = len(results)
    events: list[TaskProgressEvent] = []
    for index, result in enumerate(results, start=1):
        goal_id = str(getattr(result, "sub_goal_id", "") or f"sub-goal-{index}")
        status = str(getattr(result, "status", "unknown") or "unknown")
        progress = index / total
        events.append(TaskProgressEvent(
            run_id=run_id,
            task_id=goal_id,
            progress=progress,
            current_step=(
                f"iteration {iteration}, layer {layer_index}: "
                f"{goal_id} ({status})"
            ),
        ))
    return tuple(events)
