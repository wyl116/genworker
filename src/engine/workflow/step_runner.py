"""
StepRunner - executes a single deterministic workflow step.

Each step: LLM fills parameters -> tool call (optional) -> output.
Supports tool_choice to force specific tool usage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.common.logger import get_logger
from src.engine.protocols import LLMClient, LLMResponse, ToolExecutor, ToolResult
from src.services.llm.intent import LLMCallIntent, Purpose

logger = get_logger()


@dataclass(frozen=True)
class StepOutput:
    """Output from a single step execution."""
    content: str = ""
    structured_data: dict[str, Any] | None = None
    executed_tools: tuple["ExecutedToolCall", ...] = ()
    success: bool = True
    error: str | None = None


@dataclass(frozen=True)
class ExecutedToolCall:
    """One tool invocation performed during a step."""
    tool_name: str
    tool_input: dict[str, Any]
    result: ToolResult


class StepRunner:
    """
    Runs a single deterministic step.

    Sends prompt to LLM, optionally forces tool usage via tool_choice,
    executes tool if requested, returns result.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        tool_executor: ToolExecutor,
    ) -> None:
        self._llm = llm_client
        self._tool_executor = tool_executor

    async def run(
        self,
        prompt: str,
        task_input: str,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> StepOutput:
        """
        Execute a single step.

        Args:
            prompt: System prompt for this step.
            task_input: Input text (from previous step or user).
            tools: Tool definitions for this step.
            tool_choice: Optional tool choice constraint.

        Returns:
            StepOutput with content and optional structured data.
        """
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": task_input},
        ]

        try:
            response: LLMResponse = await self._llm.invoke(
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                intent=LLMCallIntent(
                    purpose=Purpose.TOOL_CALL,
                    requires_tools=bool(tools),
                ),
            )
        except Exception as exc:
            logger.error(f"[StepRunner] LLM invoke failed: {exc}", exc_info=True)
            return StepOutput(
                content="",
                success=False,
                error=f"LLM invocation failed: {exc}",
            )

        # If LLM made tool calls, execute them
        if response.tool_calls:
            results: list[str] = []
            structured: dict[str, Any] = {}
            executed_tools: list[ExecutedToolCall] = []

            for tc in response.tool_calls:
                try:
                    result: ToolResult = await self._tool_executor.execute(
                        tool_name=tc.tool_name,
                        tool_input=tc.tool_input,
                    )
                except Exception as exc:
                    logger.error(
                        f"[StepRunner] Tool execution failed: {tc.tool_name}: {exc}",
                        exc_info=True,
                    )
                    return StepOutput(
                        content="",
                        success=False,
                        error=f"Tool {tc.tool_name} failed: {exc}",
                    )

                if result.is_error:
                    return StepOutput(
                        content=result.content,
                        success=False,
                        error=f"Tool {tc.tool_name} returned error: {result.content}",
                    )

                results.append(result.content)
                structured[tc.tool_name] = {
                    "input": tc.tool_input,
                    "output": result.content,
                }
                executed_tools.append(ExecutedToolCall(
                    tool_name=tc.tool_name,
                    tool_input=tc.tool_input,
                    result=result,
                ))

            combined_content = "\n".join(results) if results else response.content
            return StepOutput(
                content=combined_content,
                structured_data=structured if structured else None,
                executed_tools=tuple(executed_tools),
                success=True,
            )

        # No tool calls - return LLM text directly
        return StepOutput(
            content=response.content,
            success=True,
        )
