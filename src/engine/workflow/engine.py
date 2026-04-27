"""
WorkflowEngine - deterministic sequential step execution.

Executes a list of WorkflowSteps in order, with retry support.
Each step uses StepRunner for single-step execution.
Yields StreamEvent for each step lifecycle event.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator
from uuid import uuid4

from src.common.logger import get_logger
from src.engine.checkpoint import ExecutionSnapshot, make_checkpoint_ref
from src.engine.protocols import LLMClient, ToolExecutor
from src.engine.serializer import serialize_step_result, serialize_worker_context
from src.engine.state import StepResult, UsageBudget
from src.engine.workflow.step_runner import StepRunner
from src.skills.models import WorkflowStep
from src.streaming.events import (
    BudgetExceededEvent,
    ErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    StreamEvent,
    TaskSpawnedEvent,
    TextMessageEvent,
    ToolCallEvent,
)

logger = get_logger()


def _filter_tools(
    step_tool_names: tuple[str, ...],
    available_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Filter available tools to only those declared for this step."""
    if not step_tool_names:
        return available_tools
    name_set = set(step_tool_names)
    return [t for t in available_tools if t.get("function", {}).get("name", "") in name_set]


def _build_tool_choice(
    step_tool_names: tuple[str, ...],
) -> str | dict[str, Any] | None:
    """Build tool_choice constraint for a step."""
    if len(step_tool_names) == 1:
        return {"type": "function", "function": {"name": step_tool_names[0]}}
    return None


def _build_tool_result_events(
    *,
    run_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    result: Any,
) -> tuple[StreamEvent, ...]:
    """Translate step tool results into stream events."""
    events: list[StreamEvent] = [
        ToolCallEvent(
            run_id=run_id,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_result=str(getattr(result, "content", "") or ""),
            is_error=bool(getattr(result, "is_error", False)),
        )
    ]
    metadata = dict(getattr(result, "metadata", {}) or {})
    if str(metadata.get("event_type", "") or "").strip().lower() == "task_spawned":
        events.append(TaskSpawnedEvent(
            run_id=run_id,
            task_id=str(metadata.get("task_id", "") or ""),
            task_description=str(
                metadata.get("task_description", "")
                or tool_input.get("task_description", "")
            ),
            estimated_duration=(
                str(metadata.get("estimated_duration"))
                if metadata.get("estimated_duration") is not None
                else None
            ),
        ))
    return tuple(events)


class WorkflowEngine:
    """
    Deterministic workflow engine.

    Executes steps sequentially. Each step may retry on failure.
    Yields StreamEvent frozen dataclasses.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        tool_executor: ToolExecutor,
    ) -> None:
        self._llm = llm_client
        self._tool_executor = tool_executor

    async def execute(
        self,
        steps: tuple[WorkflowStep, ...],
        task: str,
        build_step_prompt: Any,  # callable(step, previous_input) -> str
        available_tools: list[dict[str, Any]] | None = None,
        budget: UsageBudget | None = None,
        run_id: str | None = None,
        checkpoint_handle: Any | None = None,
        state_checkpointer: Any | None = None,
        resume_from: Any | None = None,
        worker_context: Any | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """
        Execute a deterministic workflow.

        Args:
            steps: Ordered workflow steps to execute.
            task: Initial user task text.
            build_step_prompt: Callable to build step prompt.
            available_tools: All available tool definitions.
            budget: Optional token budget.
            run_id: Optional run identifier.

        Yields:
            StreamEvent frozen dataclasses.
        """
        run_id = run_id or uuid4().hex
        all_tools = available_tools or []
        current_budget = budget or UsageBudget()
        runner = StepRunner(self._llm, self._tool_executor)

        yield RunStartedEvent(run_id=run_id)

        previous_input = task
        completed_results: list[StepResult] = []
        start_index = 0
        if resume_from is not None:
            for item in getattr(resume_from, "step_results", ()) or ():
                result = StepResult(
                    step_name=str(item.get("step_name", "")),
                    step_type=str(item.get("step_type", "")),
                    content=str(item.get("content", "")),
                    structured_data=item.get("structured_data"),
                    success=bool(item.get("success", True)),
                    error=item.get("error"),
                )
                completed_results.append(result)
                previous_input = result.as_input
            current_step = str(getattr(resume_from, "current_step", ""))
            if current_step:
                for index, step in enumerate(steps):
                    if step.step == current_step:
                        start_index = index + 1
                        break

        for step_index, step in enumerate(steps[start_index:], start=start_index):
            if current_budget.exceeded:
                yield BudgetExceededEvent(
                    run_id=run_id,
                    max_tokens=current_budget.max_tokens,
                    used_tokens=current_budget.used_tokens,
                )
                yield RunFinishedEvent(
                    run_id=run_id, success=True, stop_reason="budget_exceeded"
                )
                return

            yield StepStartedEvent(
                run_id=run_id,
                step_name=step.step,
                step_type=step.type.value,
            )

            step_tools = _filter_tools(step.tools, all_tools)
            tool_choice = _build_tool_choice(step.tools)
            step_prompt = build_step_prompt(step, previous_input)

            success = False
            last_error = ""

            for attempt in range(step.retry.max_attempts):
                result = await runner.run(
                    prompt=step_prompt,
                    task_input=previous_input,
                    tools=step_tools if step_tools else None,
                    tool_choice=tool_choice,
                )

                if result.success:
                    for executed in result.executed_tools:
                        for event in _build_tool_result_events(
                            run_id=run_id,
                            tool_name=executed.tool_name,
                            tool_input=executed.tool_input,
                            result=executed.result,
                        ):
                            yield event

                    step_result = StepResult(
                        step_name=step.step,
                        step_type="deterministic",
                        content=result.content,
                        structured_data=result.structured_data,
                    )
                    previous_input = step_result.as_input
                    completed_results.append(step_result)

                    if result.content:
                        yield TextMessageEvent(run_id=run_id, content=result.content)

                    if state_checkpointer is not None and checkpoint_handle is not None:
                        await state_checkpointer.save(
                            ExecutionSnapshot(
                                checkpoint_ref=make_checkpoint_ref(
                                    checkpoint_handle,
                                    round_number=step_index + 1,
                                    metadata={"current_step": step.step},
                                ),
                                budget={
                                    "max_tokens": current_budget.max_tokens,
                                    "used_tokens": current_budget.used_tokens,
                                },
                                worker_context=serialize_worker_context(worker_context) if worker_context is not None else {},
                                step_results=tuple(
                                    serialize_step_result(item) for item in completed_results
                                ),
                                current_step=step.step,
                            )
                        )

                    yield StepFinishedEvent(
                        run_id=run_id, step_name=step.step, success=True
                    )
                    success = True
                    break

                last_error = result.error or "Unknown error"
                if attempt < step.retry.max_attempts - 1:
                    logger.warning(
                        f"[WorkflowEngine] Step '{step.step}' attempt {attempt + 1} "
                        f"failed: {last_error}, retrying..."
                    )

            if not success:
                yield ErrorEvent(
                    run_id=run_id,
                    code="STEP_FAILED",
                    message=f"Step '{step.step}' failed after {step.retry.max_attempts} attempts: {last_error}",
                )
                yield StepFinishedEvent(
                    run_id=run_id, step_name=step.step, success=False
                )
                yield RunFinishedEvent(
                    run_id=run_id, success=False, stop_reason="step_failed"
                )
                return

        yield RunFinishedEvent(run_id=run_id, success=True)
