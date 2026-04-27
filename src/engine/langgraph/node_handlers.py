"""Declarative langgraph node handlers."""
from __future__ import annotations

import json
from typing import Any, Mapping

from src.engine.protocols import ToolResult
from src.services.llm.intent import LLMCallIntent, Purpose
from src.skills.models import NodeDefinition, NodeKind

from .context import NodeContext
from .models import BudgetExceededError, BudgetTracker

_HIDDEN_STATE_KEYS = frozenset({"_last_output"})


def build_node_handler(
    node: NodeDefinition,
    *,
    ctx: NodeContext,
    budget_tracker: BudgetTracker,
):
    """Build an async callable for one declarative node."""
    if node.kind == NodeKind.TOOL:
        return _build_tool_handler(node=node, ctx=ctx)
    if node.kind == NodeKind.LLM:
        return _build_llm_handler(node=node, ctx=ctx, budget_tracker=budget_tracker)
    if node.kind == NodeKind.CONDITION:
        return _build_condition_state_handler()
    if node.kind == NodeKind.INTERRUPT:
        return _build_interrupt_handler()
    raise ValueError(f"Unsupported node kind '{node.kind.value}'")


def _build_tool_handler(*, node: NodeDefinition, ctx: NodeContext):
    async def _handler(state: Mapping[str, Any]) -> dict[str, Any]:
        result = await ctx.tools.execute(node.tool, _public_state(state))
        return _merge_result(state, node.name, result)

    return _handler


def _build_llm_handler(
    *,
    node: NodeDefinition,
    ctx: NodeContext,
    budget_tracker: BudgetTracker,
):
    async def _handler(state: Mapping[str, Any]) -> dict[str, Any]:
        if budget_tracker.exceeded:
            raise BudgetExceededError("budget exceeded")
        tool_schemas = ctx.tool_schemas(node.tools)
        intent = ctx.intent(node.instruction_ref or node.name)
        if not isinstance(intent, LLMCallIntent):
            intent = LLMCallIntent(
                purpose=Purpose.TOOL_CALL if tool_schemas else Purpose.GENERATE,
                requires_tools=bool(tool_schemas),
            )
        response = await ctx.llm.invoke(
            messages=[
                {"role": "system", "content": ctx.instruction(node.instruction_ref or "general")},
                {
                    "role": "user",
                    "content": json.dumps(_public_state(state), ensure_ascii=False, sort_keys=True),
                },
            ],
            tools=tool_schemas or None,
            tool_choice=(
                {"type": "function", "function": {"name": node.tools[0]}}
                if len(node.tools) == 1 else None
            ),
            intent=intent,
        )
        budget_tracker.add_usage(getattr(getattr(response, "usage", None), "total_tokens", 0) or 0)
        next_state = dict(state)
        next_state[node.name] = str(getattr(response, "content", "") or "")
        next_state["_last_output"] = next_state[node.name]
        for tool_call in getattr(response, "tool_calls", ()) or ():
            result = await ctx.tools.execute(tool_call.tool_name, tool_call.tool_input)
            next_state = _merge_result(next_state, node.name, result)
        if budget_tracker.exceeded:
            raise BudgetExceededError("budget exceeded")
        return next_state

    return _handler


def build_condition_router(node: NodeDefinition):
    """Build the route selector for a condition node."""
    def _handler(state: Mapping[str, Any]) -> str:
        decision = state.get("_approval_decision")
        if isinstance(decision, dict) and "approved" in decision:
            approved = bool(decision.get("approved"))
            if approved and "approved" in node.route:
                return "approved"
            if not approved and "rejected" in node.route:
                return "rejected"
        for route_key in node.route:
            if bool(state.get(route_key)):
                return route_key
        last_output = str(state.get("_last_output", "") or "")
        if last_output:
            for route_key in node.route:
                if route_key in last_output:
                    return route_key
        node_value = str(state.get(node.name, "") or "")
        if node_value:
            for route_key in node.route:
                if route_key in node_value:
                    return route_key
        if node.route:
            return next(iter(node.route))
        return ""

    return _handler


def _build_condition_state_handler():
    async def _handler(state: Mapping[str, Any]) -> dict[str, Any]:
        return dict(state)

    return _handler


def _build_interrupt_handler():
    async def _handler(state: Mapping[str, Any]) -> dict[str, Any]:
        return dict(state)

    return _handler


def _public_state(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in state.items()
        if key not in _HIDDEN_STATE_KEYS
    }


def _merge_result(state: Mapping[str, Any], state_key: str, result: ToolResult) -> dict[str, Any]:
    merged = dict(state)
    metadata = dict(getattr(result, "metadata", {}) or {})
    merged.update(metadata)
    merged[state_key] = str(getattr(result, "content", "") or "")
    merged["_last_output"] = merged[state_key]
    return merged
