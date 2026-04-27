"""
ReactEngine - autonomous execution engine using the ReAct pattern.

LLM <-> Tools loop:
  1. Build system prompt via PromptBuilder
  2. Send messages to LLM
  3. If LLM requests tool calls -> execute tools -> append results -> loop
  4. If LLM returns text only -> yield final response -> done
  5. Budget exceeded -> yield BudgetExceededEvent -> stop

Uses build_managed_context() from context module for context window
management when WorkerContext is available. Falls back to character-based
trimming for backward compatibility.

Depends on LLMClient and ToolExecutor Protocols (not concrete implementations).
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, AsyncGenerator, TYPE_CHECKING
from uuid import uuid4

from src.common.logger import get_logger
from src.context.integration import build_managed_context
from src.context.models import ContextWindowConfig
from src.context.prefix_cache import StablePrefixCache
from src.engine.checkpoint import ExecutionSnapshot, make_checkpoint_ref
from src.engine.protocols import LLMClient, LLMResponse, ToolCall, ToolExecutor, ToolResult
from src.services.llm.intent import LLMCallIntent, Purpose
from src.engine.serializer import serialize_worker_context
from src.engine.state import UsageBudget, WorkerContext
from src.tools.mcp.types import ConcurrencyLevel
from src.tools.security.enforcement import normalize_workspace_path
from src.streaming.events import (
    BudgetExceededEvent,
    ErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StreamEvent,
    TaskSpawnedEvent,
    TextMessageEvent,
    ToolCallEvent,
)

if TYPE_CHECKING:
    from src.tools.mcp.server import MCPServer

logger = get_logger()

# Context window guard: max input characters before trimming (legacy fallback)
_MAX_INPUT_CHARS = 200_000


def _estimate_message_chars(msg: dict[str, Any]) -> int:
    """Estimate character count of a message dict."""
    content = msg.get("content", "")
    if isinstance(content, list):
        return sum(len(str(block)) for block in content)
    return len(str(content))


def _group_tool_call_pairs(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """
    Group messages into atomic chunks.

    An assistant message with tool_calls and subsequent tool messages
    form one group that must not be split.
    """
    groups: list[list[dict[str, Any]]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            group = [msg]
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                group.append(messages[j])
                j += 1
            groups.append(group)
            i = j
        else:
            groups.append([msg])
            i += 1
    return groups


def _trim_messages(
    messages: list[dict[str, Any]],
    max_chars: int = _MAX_INPUT_CHARS,
) -> list[dict[str, Any]]:
    """
    Legacy trimming: keeps system prompt + first user message,
    then as many recent message groups as possible.

    Used as fallback when build_managed_context is not available.
    """
    if not messages:
        return messages

    total_chars = sum(_estimate_message_chars(m) for m in messages)
    if total_chars <= max_chars:
        return messages

    preserved_head = messages[:2]
    head_chars = sum(_estimate_message_chars(m) for m in preserved_head)
    remaining_budget = max_chars - head_chars

    if remaining_budget <= 0:
        return preserved_head

    tail = messages[2:]
    groups = _group_tool_call_pairs(tail)

    kept_groups: list[list[dict[str, Any]]] = []
    used_chars = 0

    for group in reversed(groups):
        group_chars = sum(_estimate_message_chars(m) for m in group)
        if used_chars + group_chars > remaining_budget:
            break
        kept_groups.append(group)
        used_chars += group_chars

    kept_groups.reverse()
    kept_tail = [msg for group in kept_groups for msg in group]

    return preserved_head + kept_tail


def _build_tool_result_events(
    *,
    run_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    result: ToolResult,
) -> tuple[StreamEvent, ...]:
    """Translate tool result metadata into additional stream events."""
    events: list[StreamEvent] = [
        ToolCallEvent(
            run_id=run_id,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_result=result.content,
            is_error=result.is_error,
        )
    ]
    if str(result.metadata.get("event_type", "") or "").strip().lower() == "task_spawned":
        events.append(TaskSpawnedEvent(
            run_id=run_id,
            task_id=str(result.metadata.get("task_id", "") or ""),
            task_description=str(
                result.metadata.get("task_description", "")
                or tool_input.get("task_description", "")
            ),
            estimated_duration=(
                str(result.metadata.get("estimated_duration"))
                if result.metadata.get("estimated_duration") is not None
                else None
            ),
        ))
    return tuple(events)


async def _manage_context(
    messages: list[dict[str, Any]],
    worker_context: WorkerContext | None,
    context_config: ContextWindowConfig | None,
    current_round: int,
    llm_client: Any | None = None,
    memory_flush_callback: Any | None = None,
    prefix_cache: StablePrefixCache | None = None,
) -> list[dict[str, Any]]:
    """
    Apply context window management.

    Uses build_managed_context when WorkerContext and config are available,
    otherwise falls back to legacy character-based trimming.
    """
    if worker_context is not None and context_config is not None:
        try:
            assembled = await build_managed_context(
                worker_context=worker_context,
                messages=tuple(messages),
                config=context_config,
                llm_client=llm_client,
                current_round=current_round,
                contact_context=worker_context.contact_context,
                memory_orchestrator=worker_context.memory_orchestrator,
                memory_flush_callback=memory_flush_callback,
                worker_dir=worker_context.worker_dir,
                prefix_cache=prefix_cache,
            )
            result: list[dict[str, Any]] = []
            if assembled.system_prompt:
                result.append({"role": "system", "content": assembled.system_prompt})
            result.extend(assembled.messages)
            return result
        except Exception as exc:
            logger.warning(
                "[ReactEngine] build_managed_context failed, "
                "falling back to legacy trim: %s", exc,
            )

    return _trim_messages(messages)


@dataclass(frozen=True)
class _AnnotatedToolCall:
    tool_call: ToolCall
    concurrency: ConcurrencyLevel
    resource_key: str = ""


def _extract_resource_key(tool, tool_call: ToolCall) -> str:
    param = getattr(tool, "resource_key_param", "") or ""
    raw = str(tool_call.tool_input.get(param, "")).strip()
    if not raw:
        return ""
    if getattr(tool, "name", "") in {"file_write", "file_edit"}:
        return normalize_workspace_path(raw)
    return raw


def _annotate_tool_call(
    tc: ToolCall,
    mcp_server: MCPServer | None,
) -> _AnnotatedToolCall:
    if mcp_server is None:
        return _AnnotatedToolCall(tc, ConcurrencyLevel.EXCLUSIVE, "")
    tool = mcp_server.get_tool(tc.tool_name)
    if tool is None:
        return _AnnotatedToolCall(tc, ConcurrencyLevel.EXCLUSIVE, "")
    concurrency = getattr(tool, "concurrency", None)
    tool_type = getattr(tool, "tool_type", None)
    if (
        concurrency in (None, ConcurrencyLevel.EXCLUSIVE)
        and tool_type in ("read", "search")
    ):
        concurrency = ConcurrencyLevel.SAFE
    elif concurrency is None:
        concurrency = (
            ConcurrencyLevel.SAFE
            if tool_type in ("read", "search")
            else ConcurrencyLevel.EXCLUSIVE
        )
    resource_key = ""
    if concurrency == ConcurrencyLevel.PATH_SCOPED:
        resource_key = _extract_resource_key(tool, tc)
    return _AnnotatedToolCall(tc, concurrency, resource_key)


def _partition_tool_calls(
    tool_calls: list[ToolCall],
    mcp_server: MCPServer | None,
) -> list[list[ToolCall]]:
    """
    Partition tool calls into batches for execution.

    Consecutive concurrent-safe tools are grouped into one batch
    (executed in parallel). Non-concurrent-safe tools each get
    their own single-item batch (executed serially).

    Falls back to all-sequential when mcp_server is unavailable.
    """
    if not tool_calls:
        return []
    batches: list[list[ToolCall]] = []
    current_batch: list[_AnnotatedToolCall] = []
    current_keys: set[str] = set()

    def flush() -> None:
        nonlocal current_batch, current_keys
        if current_batch:
            batches.append([item.tool_call for item in current_batch])
            current_batch = []
            current_keys = set()

    for tc in tool_calls:
        annotated = _annotate_tool_call(tc, mcp_server)
        if annotated.concurrency == ConcurrencyLevel.EXCLUSIVE:
            flush()
            batches.append([tc])
            continue
        if annotated.concurrency == ConcurrencyLevel.PATH_SCOPED:
            if annotated.resource_key and annotated.resource_key in current_keys:
                flush()
            current_batch.append(annotated)
            if annotated.resource_key:
                current_keys.add(annotated.resource_key)
            continue
        current_batch.append(annotated)
    flush()
    return batches


async def _execute_single_tool(
    tc: ToolCall,
    executor: ToolExecutor,
) -> ToolResult:
    """Execute a single tool call with error handling."""
    try:
        return await executor.execute(
            tool_name=tc.tool_name,
            tool_input=tc.tool_input,
        )
    except Exception as exc:
        logger.error(
            f"[ReactEngine] Tool execution failed: {tc.tool_name}: {exc}",
            exc_info=True,
        )
        return ToolResult(content=f"Error: {exc}", is_error=True)


async def _execute_tool_batch(
    batch: list[ToolCall],
    executor: ToolExecutor,
) -> list[ToolResult]:
    """
    Execute a batch of tool calls.

    Single-item batches run directly.
    Multi-item batches run concurrently via asyncio.gather().
    """
    if len(batch) == 1:
        result = await _execute_single_tool(batch[0], executor)
        return [result]

    tasks = [_execute_single_tool(tc, executor) for tc in batch]
    return list(await asyncio.gather(*tasks))


class ReactEngine:
    """
    Autonomous execution engine using the ReAct pattern.

    Yields StreamEvent frozen dataclasses as it executes.
    Budget exceeded -> yields BudgetExceededEvent (no exception).
    """

    def __init__(
        self,
        llm_client: LLMClient,
        tool_executor: ToolExecutor,
        max_rounds: int = 10,
        mcp_server: MCPServer | None = None,
        memory_flush_callback: Any | None = None,
        prefix_cache: StablePrefixCache | None = None,
    ) -> None:
        self._llm = llm_client
        self._tool_executor = tool_executor
        self._max_rounds = max_rounds
        self._mcp_server = mcp_server
        self._memory_flush_callback = memory_flush_callback
        self._prefix_cache = prefix_cache

    async def execute(
        self,
        system_prompt: str,
        task: str,
        tools: list[dict[str, Any]] | None = None,
        budget: UsageBudget | None = None,
        run_id: str | None = None,
        worker_context: WorkerContext | None = None,
        context_config: ContextWindowConfig | None = None,
        checkpoint_handle: Any | None = None,
        state_checkpointer: Any | None = None,
        resume_from: Any | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """
        Execute the ReAct loop.

        Args:
            system_prompt: System prompt built by PromptBuilder.
            task: User task / query text.
            tools: Tool definitions in OpenAI function calling format.
            budget: Optional token budget tracker.
            run_id: Optional run identifier.
            worker_context: Optional WorkerContext for managed context.
            context_config: Optional context window config.

        Yields:
            StreamEvent frozen dataclasses.
        """
        run_id = run_id or uuid4().hex
        if resume_from is not None:
            saved_budget = getattr(resume_from, "budget", {}) or {}
            current_budget = UsageBudget(
                max_tokens=int(saved_budget.get("max_tokens", 0) or 0),
                used_tokens=int(saved_budget.get("used_tokens", 0) or 0),
            )
        else:
            current_budget = budget or UsageBudget()

        yield RunStartedEvent(run_id=run_id)

        if resume_from is not None and getattr(resume_from, "messages", ()):
            messages: list[dict[str, Any]] = list(resume_from.messages)
        else:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

        try:
            for _round in range(self._max_rounds):
                # Budget check before LLM call
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

                managed = await _manage_context(
                    messages,
                    worker_context,
                    context_config,
                    _round,
                    self._llm,
                    self._memory_flush_callback,
                    self._prefix_cache,
                )

                # Invoke LLM
                try:
                    response: LLMResponse = await self._llm.invoke(
                        messages=managed,
                        tools=tools if tools else None,
                        intent=LLMCallIntent(
                            purpose=Purpose.CHAT_TURN,
                            requires_tools=bool(tools),
                            requires_long_context=True,
                            latency_sensitive=True,
                        ),
                    )
                except Exception as exc:
                    logger.error(f"[ReactEngine] LLM invoke failed: {exc}", exc_info=True)
                    yield ErrorEvent(
                        run_id=run_id,
                        code="LLM_ERROR",
                        message=str(exc),
                    )
                    yield RunFinishedEvent(run_id=run_id, success=False, stop_reason="llm_error")
                    return

                # Update budget
                current_budget = current_budget.add_usage(response.usage.total_tokens)

                # No tool calls -> final response
                if not response.tool_calls:
                    if response.content:
                        yield TextMessageEvent(run_id=run_id, content=response.content)
                    if state_checkpointer is not None and checkpoint_handle is not None:
                        await state_checkpointer.save(
                            ExecutionSnapshot(
                                checkpoint_ref=make_checkpoint_ref(
                                    checkpoint_handle,
                                    round_number=_round + 1,
                                    message_count=len(messages),
                                    token_usage=current_budget.used_tokens,
                                    metadata={"kind": "final"},
                                ),
                                budget={
                                    "max_tokens": current_budget.max_tokens,
                                    "used_tokens": current_budget.used_tokens,
                                },
                                worker_context=serialize_worker_context(worker_context) if worker_context is not None else {},
                                messages=tuple(messages),
                            )
                        )
                    yield RunFinishedEvent(run_id=run_id, success=True)
                    return

                # Has tool calls -> execute each tool
                assistant_msg: dict[str, Any] = {"role": "assistant", "content": response.content}
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.tool_call_id,
                        "type": "function",
                        "function": {"name": tc.tool_name, "arguments": tc.tool_input},
                    }
                    for tc in response.tool_calls
                ]
                messages.append(assistant_msg)

                # Execute tool calls with concurrent batching
                batches = _partition_tool_calls(
                    response.tool_calls, self._mcp_server,
                )
                for batch in batches:
                    results = await _execute_tool_batch(
                        batch, self._tool_executor,
                    )
                    for tc, result in zip(batch, results):
                        for event in _build_tool_result_events(
                            run_id=run_id,
                            tool_name=tc.tool_name,
                            tool_input=tc.tool_input,
                            result=result,
                        ):
                            yield event
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.tool_call_id,
                            "content": result.content,
                        })
                    if state_checkpointer is not None and checkpoint_handle is not None:
                        await state_checkpointer.save(
                            ExecutionSnapshot(
                                checkpoint_ref=make_checkpoint_ref(
                                    checkpoint_handle,
                                    round_number=_round + 1,
                                    message_count=len(messages),
                                    token_usage=current_budget.used_tokens,
                                ),
                                budget={
                                    "max_tokens": current_budget.max_tokens,
                                    "used_tokens": current_budget.used_tokens,
                                },
                                worker_context=serialize_worker_context(worker_context) if worker_context is not None else {},
                                messages=tuple(messages),
                            )
                        )

            # Exceeded max rounds - do one final call without tools
            managed = await _manage_context(
                messages, worker_context, context_config, self._max_rounds, self._llm,
                self._memory_flush_callback, self._prefix_cache,
            )
            try:
                final_response = await self._llm.invoke(
                    messages=managed,
                    tools=None,
                    intent=LLMCallIntent(
                        purpose=Purpose.CHAT_TURN,
                        requires_long_context=True,
                        latency_sensitive=True,
                    ),
                )
            except Exception as exc:
                yield ErrorEvent(run_id=run_id, code="LLM_ERROR", message=str(exc))
                yield RunFinishedEvent(run_id=run_id, success=False, stop_reason="llm_error")
                return

            current_budget = current_budget.add_usage(final_response.usage.total_tokens)

            if final_response.content:
                yield TextMessageEvent(run_id=run_id, content=final_response.content)

            if state_checkpointer is not None and checkpoint_handle is not None:
                await state_checkpointer.save(
                    ExecutionSnapshot(
                        checkpoint_ref=make_checkpoint_ref(
                            checkpoint_handle,
                            round_number=self._max_rounds + 1,
                            message_count=len(messages),
                            token_usage=current_budget.used_tokens,
                            metadata={"kind": "final"},
                        ),
                        budget={
                            "max_tokens": current_budget.max_tokens,
                            "used_tokens": current_budget.used_tokens,
                        },
                        worker_context=serialize_worker_context(worker_context) if worker_context is not None else {},
                        messages=tuple(messages),
                    )
                )

            yield RunFinishedEvent(
                run_id=run_id, success=True, stop_reason="max_rounds"
            )

        except Exception as exc:
            logger.error(f"[ReactEngine] Unexpected error: {exc}", exc_info=True)
            yield ErrorEvent(run_id=run_id, code="ENGINE_ERROR", message=str(exc))
            yield RunFinishedEvent(run_id=run_id, success=False, stop_reason="error")
