"""
HybridEngine - workflow + ReAct nesting.

Executes a workflow where each step can be either:
- autonomous: delegates to ReactEngine
- deterministic: delegates to StepRunner

Steps communicate via StepResult.as_input.
Autonomous steps are detected as complete when ReactEngine yields RunFinishedEvent.
"""
from __future__ import annotations

from typing import Any, AsyncGenerator
from uuid import uuid4

from src.common.logger import get_logger
from src.engine.checkpoint import ExecutionSnapshot, make_checkpoint_ref
from src.engine.protocols import LLMClient, ToolExecutor
from src.engine.serializer import serialize_step_result, serialize_worker_context
from src.engine.react.agent import ReactEngine
from src.engine.state import StepResult, UsageBudget
from src.engine.workflow.step_runner import StepRunner
from src.skills.models import WorkflowStep, WorkflowStepType
from src.streaming.events import (
    BudgetExceededEvent,
    ErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    StreamEvent,
    TextMessageEvent,
)

logger = get_logger()


def _filter_tools(
    step_tool_names: tuple[str, ...],
    available_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Filter available tools to those declared for this step."""
    if not step_tool_names:
        return available_tools
    name_set = set(step_tool_names)
    return [t for t in available_tools if t.get("function", {}).get("name", "") in name_set]


class HybridEngine:
    """
    Hybrid execution engine combining workflow structure with ReAct autonomy.

    Each step is dispatched based on its type:
    - autonomous -> ReactEngine (reused instance)
    - deterministic -> StepRunner

    Steps communicate through StepResult.as_input.
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
        build_autonomous_prompt: Any,  # callable(step) -> str
        available_tools: list[dict[str, Any]] | None = None,
        budget: UsageBudget | None = None,
        run_id: str | None = None,
        checkpoint_handle: Any | None = None,
        state_checkpointer: Any | None = None,
        resume_from: Any | None = None,
        worker_context: Any | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """
        Execute a hybrid workflow.

        Args:
            steps: Ordered workflow steps.
            task: Initial user task text.
            build_step_prompt: Callable for deterministic step prompts.
            build_autonomous_prompt: Callable for autonomous step prompts.
            available_tools: All available tool definitions.
            budget: Optional token budget.
            run_id: Optional run identifier.

        Yields:
            StreamEvent frozen dataclasses.
        """
        run_id = run_id or uuid4().hex
        all_tools = available_tools or []
        current_budget = budget or UsageBudget()

        yield RunStartedEvent(run_id=run_id)

        previous_result = StepResult(
            step_name="input", step_type="input", content=task
        )
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
                previous_result = result
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

            if step.type == WorkflowStepType.AUTONOMOUS:
                step_result = await self._execute_autonomous_step(
                    step=step,
                    previous_result=previous_result,
                    build_autonomous_prompt=build_autonomous_prompt,
                    available_tools=all_tools,
                    budget=current_budget,
                    run_id=run_id,
                )
            else:
                step_result = await self._execute_deterministic_step(
                    step=step,
                    previous_result=previous_result,
                    build_step_prompt=build_step_prompt,
                    available_tools=all_tools,
                    run_id=run_id,
                )

            if not step_result.success:
                yield ErrorEvent(
                    run_id=run_id,
                    code="STEP_FAILED",
                    message=f"Step '{step.step}' failed: {step_result.error}",
                )
                yield StepFinishedEvent(
                    run_id=run_id, step_name=step.step, success=False
                )
                yield RunFinishedEvent(
                    run_id=run_id, success=False, stop_reason="step_failed"
                )
                return

            if step_result.content:
                yield TextMessageEvent(run_id=run_id, content=step_result.content)

            yield StepFinishedEvent(
                run_id=run_id, step_name=step.step, success=True
            )

            previous_result = step_result
            completed_results.append(step_result)
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

        yield RunFinishedEvent(run_id=run_id, success=True)

    async def _execute_autonomous_step(
        self,
        step: WorkflowStep,
        previous_result: StepResult,
        build_autonomous_prompt: Any,
        available_tools: list[dict[str, Any]],
        budget: UsageBudget,
        run_id: str,
    ) -> StepResult:
        """Execute an autonomous step via ReactEngine."""
        engine = ReactEngine(
            llm_client=self._llm,
            tool_executor=self._tool_executor,
            max_rounds=step.max_rounds or 5,
        )

        system_prompt = build_autonomous_prompt(step)
        step_tools = _filter_tools(step.tools, available_tools)
        last_text_content = ""

        # ReactEngine yields its own RunStarted/RunFinished - we consume
        # them internally and extract the final text content
        async for event in engine.execute(
            system_prompt=system_prompt,
            task=previous_result.as_input,
            tools=step_tools if step_tools else None,
            budget=budget,
            run_id=f"{run_id}_{step.step}",
        ):
            if isinstance(event, TextMessageEvent):
                last_text_content = event.content
            elif isinstance(event, ErrorEvent):
                return StepResult(
                    step_name=step.step,
                    step_type="autonomous",
                    content="",
                    success=False,
                    error=event.message,
                )
            # We don't re-yield ReactEngine's RunStarted/RunFinished
            # (the HybridEngine emits its own step lifecycle events)

        return StepResult(
            step_name=step.step,
            step_type="autonomous",
            content=last_text_content,
        )

    async def _execute_deterministic_step(
        self,
        step: WorkflowStep,
        previous_result: StepResult,
        build_step_prompt: Any,
        available_tools: list[dict[str, Any]],
        run_id: str,
    ) -> StepResult:
        """Execute a deterministic step via StepRunner with retry."""
        runner = StepRunner(self._llm, self._tool_executor)
        step_tools = _filter_tools(step.tools, available_tools)
        step_prompt = build_step_prompt(step, previous_result.as_input)

        tool_choice = None
        if len(step.tools) == 1:
            tool_choice = {"type": "function", "function": {"name": step.tools[0]}}

        last_error = ""

        for attempt in range(step.retry.max_attempts):
            result = await runner.run(
                prompt=step_prompt,
                task_input=previous_result.as_input,
                tools=step_tools if step_tools else None,
                tool_choice=tool_choice,
            )

            if result.success:
                return StepResult(
                    step_name=step.step,
                    step_type="deterministic",
                    content=result.content,
                    structured_data=result.structured_data,
                )

            last_error = result.error or "Unknown error"
            if attempt < step.retry.max_attempts - 1:
                logger.warning(
                    f"[HybridEngine] Step '{step.step}' attempt {attempt + 1} "
                    f"failed: {last_error}, retrying..."
                )

        return StepResult(
            step_name=step.step,
            step_type="deterministic",
            content="",
            success=False,
            error=f"Failed after {step.retry.max_attempts} attempts: {last_error}",
        )
