"""Bootstrap builder helpers extracted from the composition root."""
from __future__ import annotations

import importlib
import json
import warnings

from src.common.logger import get_logger
from src.worker.task import create_task_manifest

logger = get_logger()


class _RouterAdapter:
    """Bridge SubAgentExecutor to WorkerRouter after deferred injection."""

    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id
        self._router = None

    def set_router(self, router) -> None:
        self._router = router

    async def execute_subagent(self, context) -> str:
        if self._router is None:
            raise RuntimeError(
                "_RouterAdapter: router not set - call set_router() after WorkerRouter creation"
            )

        chunks: list[str] = []
        run_failed = False
        failure_message = ""
        route_kwargs = {
            "task": context.sub_goal.description,
            "tenant_id": self._tenant_id,
            "worker_id": context.delegate_worker_id or context.parent_worker_id,
            "skill_id": context.skill_id,
            "preferred_skill_ids": context.preferred_skill_ids,
            "tool_whitelist": context.tool_sandbox or None,
            "subagent_depth": 1,
            "max_rounds_override": context.max_rounds,
        }
        if getattr(context, "pre_script", None) is not None:
            route_kwargs["manifest"] = create_task_manifest(
                worker_id=context.delegate_worker_id or context.parent_worker_id,
                tenant_id=self._tenant_id,
                skill_id=context.skill_id or "",
                preferred_skill_ids=context.preferred_skill_ids,
                task_description=context.sub_goal.description,
                pre_script=context.pre_script,
            )
        async for event in self._router.route_stream(**route_kwargs):
            content = getattr(event, "content", "")
            if content:
                chunks.append(content)
            if getattr(event, "event_type", "") == "ERROR":
                run_failed = True
                failure_message = getattr(event, "message", "")
            if getattr(event, "event_type", "") == "RUN_FINISHED" and not getattr(
                event, "success", True,
            ):
                run_failed = True
                failure_message = (
                    getattr(event, "stop_reason", "")
                    or failure_message
                )
        if run_failed:
            raise RuntimeError(failure_message or "Subagent run failed")
        return "".join(chunks).strip()


def build_planning_stack(
    *,
    tenant_id: str,
    event_bus,
    llm_client,
):
    """Build deferred subagent execution and planning components."""
    adapter = _RouterAdapter(tenant_id=tenant_id)
    subagent_executor = build_subagent_executor(
        adapter=adapter,
        event_bus=event_bus,
    )
    enhanced_planning_executor = build_enhanced_planning_executor(
        llm_client=llm_client,
        subagent_executor=subagent_executor,
    )
    return adapter, subagent_executor, enhanced_planning_executor


def build_langgraph_stack(
    *,
    workspace_root,
    tool_executor,
    llm_client,
    inbox_store,
):
    """Build the langgraph checkpointer and engine pair."""
    try:
        checkpointer_module = importlib.import_module("src.engine.langgraph.checkpointer")
        engine_module = importlib.import_module("src.engine.langgraph.engine")
    except ImportError as exc:
        logger.warning("[bootstrap] LangGraph stack unavailable: %s", exc)
        return None, None

    LangGraphCheckpointer = getattr(checkpointer_module, "LangGraphCheckpointer")
    LangGraphEngine = getattr(engine_module, "LangGraphEngine")

    checkpointer = LangGraphCheckpointer(workspace_root=workspace_root)
    engine = LangGraphEngine(
        workspace_root=workspace_root,
        checkpointer=checkpointer,
        tool_executor=tool_executor,
        llm_client=llm_client,
        inbox_store=inbox_store,
    )
    return checkpointer, engine


def build_unavailable_llm_client(settings, config_manager=None):
    """Return an LLM client that reports router/config unavailability explicitly."""
    from src.engine.protocols import LLMResponse

    if config_manager is None and settings is not None:
        from src.services.llm.config_source import (
            MissingInjectedConfigError,
            build_litellm_config_source,
        )

        try:
            config_manager = build_litellm_config_source(settings)
        except MissingInjectedConfigError:
            raise
        except Exception:
            config_manager = None

    default_tier = None
    model_group = None
    if (
        config_manager is not None
        and hasattr(config_manager, "get_default_tier")
        and hasattr(config_manager, "get_tier_model")
    ):
        try:
            default_tier = config_manager.get_default_tier()
            model_group = config_manager.get_tier_model(default_tier)
        except Exception:
            default_tier = None
            model_group = None

    class UnavailableLLMClient:
        async def invoke(
            self,
            messages,
            tools=None,
            tool_choice=None,
            system_blocks=None,
            intent=None,
        ):
            if model_group:
                return LLMResponse(
                    content=(
                        "LLM Error: LiteLLM Router unavailable; "
                        f"configured tier '{default_tier}' maps to '{model_group}', "
                        "but direct provider invocation is disabled. "
                        "Initialize LiteLLM Router to use litellm.json fallbacks."
                    )
                )
            return LLMResponse(
                content=(
                    "LLM Error: LiteLLM Router unavailable and no valid default_tier/model "
                    "could be resolved from litellm.json."
                )
            )

    return UnavailableLLMClient()


def build_direct_llm_client(settings, config_manager=None):
    """Deprecated compatibility wrapper for historical imports."""
    warnings.warn(
        "build_direct_llm_client() is deprecated; use build_unavailable_llm_client()",
        DeprecationWarning,
        stacklevel=2,
    )
    return build_unavailable_llm_client(
        settings=settings,
        config_manager=config_manager,
    )


def build_subagent_executor(adapter, event_bus):
    from src.worker.planning.subagent.executor import SubAgentExecutor

    return SubAgentExecutor(
        task_executor=adapter,
        event_bus=event_bus,
    )


def build_enhanced_planning_executor(llm_client, subagent_executor):
    from src.worker.planning.decomposer import Decomposer
    from src.worker.planning.enhanced_executor import EnhancedPlanningExecutor
    from src.worker.planning.reflector import Reflector
    from src.worker.planning.strategy_selector import StrategySelector

    return EnhancedPlanningExecutor(
        decomposer=Decomposer(llm_client=llm_client),
        strategy_selector=StrategySelector(llm_client=llm_client),
        reflector=Reflector(llm_client=llm_client),
        subagent_executor=subagent_executor,
    )


def parse_tool_args(args_str):
    """Parse tool call arguments (may be JSON string or dict)."""
    if isinstance(args_str, dict):
        return args_str
    try:
        return json.loads(args_str) if args_str else {}
    except (json.JSONDecodeError, TypeError):
        return {"raw": args_str}


def build_tool_executor(mcp_server, context):
    """Build a ToolExecutor from MCP server tools."""
    import inspect

    from src.engine.protocols import ToolResult as EngineToolResult
    from src.tools.pipeline import ToolCallContext
    from src.tools.runtime_scope import ExecutionScope
    from src.tools.runtime_scope import ExecutionScopeProvider
    from src.tools.security.models import EnforcementConstraint
    from src.worker.trust_gate import WorkerTrustGate

    # Preserve legacy/bootstrap callers that do not enter through request-scoped routing.
    fallback_scope = None
    if mcp_server is not None:
        fallback_scope = ExecutionScope(
            tenant_id=str(context.get_state("tenant_id", "")),
            worker_id=str(context.get_state("worker_id", "")),
            skill_id=str(context.get_state("skill_id", "")),
            trust_gate=context.get_state(
                "trust_gate",
                WorkerTrustGate(
                    trusted=True,
                    bash_enabled=True,
                    semantic_search_enabled=True,
                ),
            ),
            allowed_tool_names=frozenset(
                tool.name for tool in mcp_server.get_all_tools()
            ),
            constraint=EnforcementConstraint(max_execution_time=30.0),
        )

    class MCPToolExecutorAdapter:
        def __init__(
            self,
            server,
            tool_pipeline=None,
            context_provider=None,
            default_scope=None,
        ):
            self._server = server
            self._pipeline = tool_pipeline
            self._context_provider = context_provider
            self._default_scope = default_scope

        @property
        def pipeline(self):
            return self._pipeline

        async def execute(self, tool_name, tool_input):
            if self._server is None:
                return EngineToolResult(
                    content=f"Tool '{tool_name}' not available (MCP not initialized)",
                    is_error=True,
                )
            scope = self._context_provider() if self._context_provider else None
            if scope is None:
                scope = self._default_scope
            tool = None
            if scope is not None:
                tool = getattr(scope, "scoped_tools", {}).get(tool_name)
            if tool is None:
                tool = self._server.get_tool(tool_name)
            if tool is None:
                return EngineToolResult(
                    content=f"Tool '{tool_name}' not found",
                    is_error=True,
                )
            try:
                if self._pipeline is not None:
                    pipeline_result = await self._pipeline.execute(
                        ToolCallContext.from_scope(
                            scope,
                            tool_name=tool_name,
                            tool_input=tool_input if isinstance(tool_input, dict) else {},
                            risk_level=str(getattr(tool, "risk_level", "medium")),
                            tool=tool,
                        )
                    )
                    return EngineToolResult(
                        content=pipeline_result.content,
                        is_error=pipeline_result.is_error,
                        metadata=dict(getattr(pipeline_result, "metadata", {}) or {}),
                    )
                if isinstance(tool_input, dict):
                    result = tool.handler(**tool_input)
                else:
                    result = tool.handler(tool_input)
                if inspect.isawaitable(result):
                    result = await result
                if isinstance(result, str):
                    return EngineToolResult(content=result)
                if hasattr(result, "content"):
                    return EngineToolResult(
                        content=str(getattr(result, "content", "")),
                        is_error=bool(getattr(result, "is_error", False)),
                        metadata=dict(getattr(result, "metadata", {}) or {}),
                    )
                return EngineToolResult(
                    content=str(result),
                    metadata=dict(getattr(result, "metadata", {}) or {}),
                )
            except Exception as exc:
                return EngineToolResult(content=f"Tool error: {exc}", is_error=True)

    scope_provider = context.get_state("execution_scope_provider")
    if scope_provider is None:
        scope_provider = ExecutionScopeProvider()
        set_state = getattr(context, "set_state", None)
        if callable(set_state):
            set_state("execution_scope_provider", scope_provider)

    tool_pipeline = build_tool_pipeline(
        mcp_server,
        workspace_root=str(context.get_state("workspace_root", "workspace")),
    )
    return MCPToolExecutorAdapter(
        mcp_server,
        tool_pipeline=tool_pipeline,
        context_provider=scope_provider.current,
        default_scope=fallback_scope,
    )


def build_tool_pipeline(mcp_server, workspace_root: str):
    """Build the tool execution security pipeline for MCP tools."""
    if mcp_server is None:
        return None

    from src.tools.builtin.bash_security import BashSecurityHook
    from src.tools.middlewares.sanitize import SanitizeMiddleware
    from src.tools.pipeline import ToolPipeline
    from src.tools.security.enforcement import NetworkFence, ResourceFence
    from src.tools.security.policy import PolicyEvaluator
    from src.tools.sandbox import ScopedToolExecutor

    all_tools = tuple(mcp_server.get_all_tools())
    allowed_tools = {tool.name: tool for tool in all_tools}
    return ToolPipeline(
        executor=ScopedToolExecutor(allowed_tools=allowed_tools),
        middlewares=(SanitizeMiddleware(),),
        hooks=(BashSecurityHook(),),
        policy=PolicyEvaluator(require_scope=True),
        fences=(ResourceFence(workspace_root=workspace_root), NetworkFence()),
    )
