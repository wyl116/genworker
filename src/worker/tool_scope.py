"""Tool runtime scope construction for worker execution."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.tools.formatters import ToolResult
from src.tools.builtin.task_store import TaskStore
from src.tools.builtin.task_tools import create_task_tools
from src.tools.mcp.tool import Tool
from src.tools.mcp.types import ConcurrencyLevel, MCPCategory, RiskLevel, ToolType
from src.tools.runtime_scope import ExecutionScope
from src.tools.security.models import EnforcementConstraint
from src.worker.tool_sandbox import compute_available_tools

LLM_HIDDEN_TAG = "hidden_from_llm"


@dataclass(frozen=True)
class ToolRuntimeBundle:
    """Resolved tool set and execution scope for one worker run."""

    available_tools: tuple[Any, ...]
    tool_schemas: list[dict[str, Any]]
    scope: ExecutionScope
    subagent_enabled: bool


def build_tool_runtime_bundle(
    *,
    worker,
    tenant,
    trust_gate,
    all_tools: tuple[Any, ...],
    worker_router: Any,
    subagent_executor: Any | None,
    create_subagent_tool_fn: Any | None,
    task_spawner: Any | None,
    conversation_session: Any | None,
    session_search_index: Any | None,
    tool_whitelist: tuple[str, ...] | None,
    subagent_depth: int,
    parent_task_id: str,
) -> ToolRuntimeBundle:
    """Build the per-run tool set plus execution scope."""
    available_tools = compute_available_tools(
        worker=worker,
        tenant=tenant,
        trust_gate=trust_gate,
        all_tools=all_tools,
    )
    if tool_whitelist is not None:
        allowed = set(tool_whitelist)
        available_tools = tuple(
            tool for tool in available_tools if tool.name in allowed
        )

    scoped_tools: dict[str, Tool] = {}
    subagent_enabled = (
        subagent_executor is not None
        and create_subagent_tool_fn is not None
        and trust_gate.trusted
        and subagent_depth == 0
    )
    if subagent_enabled:
        subagent_tool = create_subagent_tool_fn(
            executor=subagent_executor,
            worker_id=worker.worker_id,
            parent_task_id=parent_task_id,
            tool_sandbox=tuple(t.name for t in available_tools),
        )
        available_tools = (*available_tools, subagent_tool)
        scoped_tools[subagent_tool.name] = subagent_tool

    if (
        task_spawner is not None
        and conversation_session is not None
        and _tool_allowed("spawn_task", tool_whitelist)
    ):
        spawn_task_tool = create_spawn_task_tool(
            task_spawner=task_spawner,
            session=conversation_session,
        )
        available_tools = (*available_tools, spawn_task_tool)
        scoped_tools[spawn_task_tool.name] = spawn_task_tool

    run_task_store = TaskStore()
    run_task_tools = create_task_tools(run_task_store)
    filtered_task_tools = tuple(
        tool for tool in run_task_tools if _tool_allowed(tool.name, tool_whitelist)
    )
    available_tools = (*available_tools, *filtered_task_tools)
    scoped_tools.update({tool.name: tool for tool in filtered_task_tools})

    if (
        session_search_index is not None
        and trust_gate.semantic_search_enabled
        and _tool_allowed("session_search", tool_whitelist)
    ):
        search_tool = create_session_search_tool(
            search_index=session_search_index,
            tenant_id=tenant.tenant_id,
            worker_id=worker.worker_id,
        )
        available_tools = (*available_tools, search_tool)
        scoped_tools[search_tool.name] = search_tool

    if (
        trust_gate.trusted
        and subagent_depth < 2
        and _tool_allowed("delegate_to_worker", tool_whitelist)
    ):
        delegate_whitelist = tuple(
            dict.fromkeys((*tuple(tool.name for tool in available_tools), "delegate_to_worker"))
        )
        delegate_tool = create_delegate_to_worker_tool(
            worker_router=worker_router,
            tenant_id=tenant.tenant_id,
            source_worker_id=worker.worker_id,
            inherited_tool_names=delegate_whitelist,
            delegation_depth=subagent_depth,
        )
        available_tools = (*available_tools, delegate_tool)
        scoped_tools[delegate_tool.name] = delegate_tool

    scope = ExecutionScope(
        tenant_id=tenant.tenant_id,
        worker_id=worker.worker_id,
        skill_id="",
        trust_gate=trust_gate,
        allowed_tool_names=frozenset(tool.name for tool in available_tools),
        constraint=EnforcementConstraint(max_execution_time=30.0),
        scoped_tools=scoped_tools,
    )
    return ToolRuntimeBundle(
        available_tools=available_tools,
        tool_schemas=[
            tool.to_openai_schema()
            for tool in available_tools
            if LLM_HIDDEN_TAG not in tool.tags
        ],
        scope=scope,
        subagent_enabled=subagent_enabled,
    )


def with_skill_scope(
    bundle: ToolRuntimeBundle,
    *,
    skill_id: str,
) -> ExecutionScope:
    """Copy a bundle scope with the resolved skill id."""
    return ExecutionScope(
        tenant_id=bundle.scope.tenant_id,
        worker_id=bundle.scope.worker_id,
        skill_id=skill_id,
        trust_gate=bundle.scope.trust_gate,
        allowed_tool_names=bundle.scope.allowed_tool_names,
        constraint=bundle.scope.constraint,
        scoped_tools=bundle.scope.scoped_tools,
    )


def _tool_allowed(
    tool_name: str,
    tool_whitelist: tuple[str, ...] | None,
) -> bool:
    """Check whether a runtime-injected tool is permitted by whitelist."""
    if tool_whitelist is None:
        return True
    return tool_name in set(tool_whitelist)


def create_spawn_task_tool(
    *,
    task_spawner: Any,
    session: Any,
) -> Tool:
    """Create the conversation-scoped spawn_task tool."""
    from src.conversation.task_spawner import SpawnTaskInput

    async def handler(
        task_description: str,
        context: str = "",
        skill_hint: str = "",
    ) -> ToolResult:
        result = await task_spawner.execute(
            SpawnTaskInput(
                task_description=task_description,
                context=context,
                skill_hint=skill_hint or None,
            ),
            session=session,
        )
        if result.status != "accepted":
            return ToolResult(content=result.message, is_error=True)
        return ToolResult(
            content=result.message,
            metadata={
                "event_type": "task_spawned",
                "task_id": result.task_id,
                "task_description": task_description,
            },
        )

    return Tool(
        name="spawn_task",
        description=(
            "Create a background task tied to the current conversation when the "
            "work will take longer than the current round."
        ),
        handler=handler,
        parameters={
            "task_description": {
                "type": "string",
                "description": "Clear background task description with enough execution detail",
            },
            "context": {
                "type": "string",
                "description": "Relevant context to preserve for the background task",
            },
            "skill_hint": {
                "type": "string",
                "description": "Optional skill hint for routing the background task",
            },
        },
        required_params=("task_description",),
        tool_type=ToolType.WRITE,
        category=MCPCategory.SPECIALIZED,
        risk_level=RiskLevel.LOW,
        concurrency=ConcurrencyLevel.EXCLUSIVE,
        tags=frozenset({"task", "background", "conversation"}),
    )


def create_delegate_to_worker_tool(
    *,
    worker_router: Any,
    tenant_id: str,
    source_worker_id: str,
    inherited_tool_names: tuple[str, ...],
    delegation_depth: int,
) -> Tool:
    """Create a trusted cross-worker delegation tool."""

    async def handler(
        target_worker: str,
        task: str,
        context: str = "",
    ) -> ToolResult:
        parts: list[str] = []
        error_message = ""
        async for event in worker_router.route_stream(
            task=task,
            tenant_id=tenant_id,
            worker_id=target_worker,
            task_context=context,
            tool_whitelist=inherited_tool_names,
            subagent_depth=delegation_depth + 1,
        ):
            event_type = getattr(event, "event_type", "")
            if event_type == "TEXT_MESSAGE":
                content = getattr(event, "content", "")
                if content:
                    parts.append(content)
            elif event_type == "ERROR":
                error_message = getattr(event, "message", "") or "Delegation failed"
        if error_message:
            return ToolResult(content=error_message, is_error=True)
        content = "\n\n".join(part.strip() for part in parts if str(part).strip())
        if not content:
            content = f"Task completed by {target_worker}."
        return ToolResult(content=content)

    return Tool(
        name="delegate_to_worker",
        description=(
            "Delegate a task to another worker when it clearly falls outside the "
            "current worker's specialization."
        ),
        handler=handler,
        parameters={
            "target_worker": {
                "type": "string",
                "description": "Target worker ID",
            },
            "task": {
                "type": "string",
                "description": "Self-contained task description for the target worker",
            },
            "context": {
                "type": "string",
                "description": "Optional additional context passed to the target worker",
            },
        },
        required_params=("target_worker", "task"),
        tool_type=ToolType.WRITE,
        category=MCPCategory.SPECIALIZED,
        risk_level=RiskLevel.MEDIUM,
        concurrency=ConcurrencyLevel.EXCLUSIVE,
        tags=frozenset({"delegation", "worker", "collaboration", source_worker_id}),
    )


def create_session_search_tool(
    *,
    search_index: Any,
    tenant_id: str,
    worker_id: str,
) -> Tool:
    async def handler(
        query: str,
        date_start: str = "",
        date_end: str = "",
        limit: int = 10,
    ) -> str:
        result = await search_index.search(
            query=query,
            tenant_id=tenant_id,
            worker_id=worker_id,
            date_start=date_start,
            date_end=date_end,
            limit=limit,
        )
        if not result.hits:
            return f"No session results for: {query}"
        lines = [f"Session search results for: {query}"]
        for hit in result.hits:
            lines.append(
                f"- [{hit.created_at}] {hit.role} {hit.snippet or hit.content}"
            )
        return "\n".join(lines)

    return Tool(
        name="session_search",
        description="Search the current worker's raw session history",
        handler=handler,
        parameters={
            "query": {"type": "string", "description": "FTS search query"},
            "date_start": {"type": "string", "description": "Inclusive start date filter"},
            "date_end": {"type": "string", "description": "Inclusive end date filter"},
            "limit": {"type": "integer", "description": "Maximum results to return"},
        },
        required_params=("query",),
        tool_type=ToolType.SEARCH,
        category=MCPCategory.SPECIALIZED,
        risk_level=RiskLevel.LOW,
        concurrency=ConcurrencyLevel.SAFE,
        tags=frozenset({"session", "search", "fts"}),
    )
