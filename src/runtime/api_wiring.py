"""API wiring implementation extracted from the bootstrap composition root."""
from __future__ import annotations

from pathlib import Path


async def initialize_api_wiring(context) -> bool:
    """Create the router, dispatcher and task runtime objects for the app."""
    try:
        deps = resolve_api_wiring_dependencies(context)
        workspace_root = deps["workspace_root"]
        tenant_loader = deps["tenant_loader"]
        worker_registry = deps["worker_registry"]
        mcp_server = deps["mcp_server"]
        openviking_client = deps["openviking_client"]
        event_bus = deps["event_bus"]
        episode_lock = deps["episode_lock"]
        memory_orchestrator = build_memory_orchestrator(
            openviking_client=openviking_client,
            openviking_scope_prefix=deps["openviking_scope_prefix"],
            event_bus=event_bus,
        )
        llm_client = build_llm_client(context)
        tool_executor = build_tool_executor(mcp_server, context)
        register_langgraph_approval_event_types(workspace_root)
        post_run_handler = build_post_run_handler(
            workspace_root=workspace_root,
            llm_client=llm_client,
            episode_lock=episode_lock,
            memory_orchestrator=memory_orchestrator,
            openviking_client=openviking_client,
            openviking_scope_prefix=deps["openviking_scope_prefix"],
            suggestion_store=context.get_state("suggestion_store"),
            goal_lock_registry=context.get_state("goal_lock_registry"),
        )
        from src.engine.checkpoint import StateCheckpointer

        state_checkpointer = StateCheckpointer(workspace_root=workspace_root)
        error_feedback_handler = build_error_feedback_handler(
            workspace_root=workspace_root,
            episode_lock=episode_lock,
            memory_orchestrator=memory_orchestrator,
            openviking_client=openviking_client,
            openviking_scope_prefix=deps["openviking_scope_prefix"],
        )
        memory_flush_callback = build_memory_flush_callback(
            workspace_root=workspace_root,
            memory_orchestrator=memory_orchestrator,
            episode_lock=episode_lock,
            openviking_client=openviking_client,
            openviking_scope_prefix=deps["openviking_scope_prefix"],
        )
        (
            subagent_adapter,
            subagent_executor,
            enhanced_planning_executor,
        ) = build_planning_stack(
            tenant_id=context.get_state("tenant_id", "demo"),
            event_bus=event_bus,
            llm_client=llm_client,
        )
        (
            langgraph_checkpointer,
            langgraph_engine,
        ) = build_langgraph_stack(
            workspace_root=workspace_root,
            tool_executor=tool_executor,
            llm_client=llm_client,
            inbox_store=context.get_state("session_inbox_store"),
        )
        engine_dispatcher = build_engine_dispatcher(
            llm_client=llm_client,
            tool_executor=tool_executor,
            mcp_server=mcp_server,
            memory_flush_callback=memory_flush_callback,
            enhanced_planning_executor=enhanced_planning_executor,
            state_checkpointer=state_checkpointer,
            langgraph_engine=langgraph_engine,
        )
        task_store = build_task_store(workspace_root)
        task_runner = build_task_runner(
            engine_dispatcher=engine_dispatcher,
            task_store=task_store,
            post_run_handler=post_run_handler,
            error_feedback_handler=error_feedback_handler,
            state_checkpointer=state_checkpointer,
            tool_pipeline=getattr(tool_executor, "pipeline", None),
        )
        worker_router = build_worker_router(
            worker_registry=worker_registry,
            tenant_loader=tenant_loader,
            task_runner=task_runner,
            mcp_server=mcp_server,
            workspace_root=workspace_root,
            subagent_executor=subagent_executor,
            memory_orchestrator=memory_orchestrator,
            execution_scope_provider=context.get_state("execution_scope_provider"),
        )
        subagent_adapter.set_router(worker_router)

        context.set_state("engine_dispatcher", engine_dispatcher)
        context.set_state("task_store", task_store)
        context.set_state("worker_router", worker_router)
        context.set_state("llm_client", llm_client)
        context.set_state("tool_executor", tool_executor)
        context.set_state("state_checkpointer", state_checkpointer)
        context.set_state("langgraph_checkpointer", langgraph_checkpointer)
        context.set_state("langgraph_engine", langgraph_engine)
        context.set_state("episode_lock", episode_lock)
        context.set_state("memory_orchestrator", memory_orchestrator)
        context.set_state("subagent_executor", subagent_executor)
        context.set_state(
            "enhanced_planning_executor", enhanced_planning_executor,
        )
        context.set_state(
            "engine_registry",
            build_engine_registry(
                llm_client=llm_client,
                tool_executor=tool_executor,
                enhanced_planning_executor=enhanced_planning_executor,
                langgraph_checkpointer=langgraph_checkpointer,
                langgraph_engine=langgraph_engine,
            ),
        )

        from src.common.logger import get_logger

        get_logger().info("[ApiWiring] WorkerRouter created and stored in context")
        return True

    except Exception as exc:
        from src.common.logger import get_logger

        get_logger().error(f"[ApiWiring] Failed: {exc}", exc_info=True)
        context.record_error("api_wiring", str(exc))
        return False


def build_engine_registry(
    *,
    llm_client,
    tool_executor,
    enhanced_planning_executor,
    langgraph_checkpointer,
    langgraph_engine,
) -> dict[str, dict[str, bool]]:
    """Build a lightweight engine readiness registry for /health."""
    core_ready = llm_client is not None and tool_executor is not None
    return {
        "autonomous": {
            "ready": core_ready,
        },
        "deterministic": {
            "ready": core_ready,
        },
        "hybrid": {
            "ready": core_ready,
        },
        "planning": {
            "ready": enhanced_planning_executor is not None,
        },
        "langgraph": {
            "import_ok": langgraph_engine is not None,
            "checkpointer_ok": langgraph_checkpointer is not None,
        },
    }


def register_langgraph_approval_event_types(workspace_root) -> tuple[str, ...]:
    """Pre-register approval event types from langgraph skills for restart safety."""
    from src.channels.commands.approval_events import register_approval_event_type
    from src.skills.loader import SkillLoader
    from src.skills.models import NodeKind, StrategyMode

    root = Path(workspace_root)
    loader = SkillLoader()
    skills = loader.scan_multiple((
        root / "system" / "skills",
        root / "tenants",
    ))
    registered: list[str] = []
    seen: set[str] = set()
    for skill in skills:
        if skill.strategy.mode != StrategyMode.LANGGRAPH or skill.strategy.graph is None:
            continue
        for node in skill.strategy.graph.nodes:
            if node.kind != NodeKind.INTERRUPT:
                continue
            event_type = str(node.inbox_event_type or "").strip()
            if not event_type or event_type in seen:
                continue
            register_approval_event_type(event_type)
            seen.add(event_type)
            registered.append(event_type)
    return tuple(registered)


def resolve_api_wiring_dependencies(context) -> dict[str, object]:
    """Resolve the shared dependency set needed by api wiring."""
    from pathlib import Path

    from src.common.tenant import TenantLoader
    from src.memory.episodic.store import IndexFileLock

    workspace_root = Path(context.get_state("workspace_root", "workspace"))
    return {
        "workspace_root": workspace_root,
        "tenant_loader": context.get_state("tenant_loader", TenantLoader(workspace_root)),
        "worker_registry": context.get_state("worker_registry"),
        "mcp_server": context.get_state("mcp_server"),
        "openviking_client": context.get_state("openviking_client"),
        "openviking_scope_prefix": getattr(
            context.settings,
            "openviking_scope_prefix",
            "viking://",
        ),
        "event_bus": context.get_state("event_bus"),
        "episode_lock": IndexFileLock(),
    }


def build_memory_orchestrator(
    *,
    openviking_client,
    openviking_scope_prefix: str,
    event_bus,
):
    """Build MemoryOrchestrator and its providers."""
    from src.memory.orchestrator import MemoryOrchestrator
    from src.memory.provider import (
        EpisodicMemoryProvider,
        PreferenceMemoryProvider,
        SemanticMemoryProvider,
    )

    providers = [
        SemanticMemoryProvider(
            openviking_client,
            scope_prefix=openviking_scope_prefix,
        ),
        EpisodicMemoryProvider(
            openviking_client,
            base_dir=None,
            scope_prefix=openviking_scope_prefix,
        ),
    ]
    providers.append(PreferenceMemoryProvider())
    return MemoryOrchestrator(
        providers=tuple(providers),
        event_bus=event_bus,
    )


def build_engine_dispatcher(
    *,
    llm_client,
    tool_executor,
    mcp_server,
    memory_flush_callback,
    enhanced_planning_executor,
    state_checkpointer,
    langgraph_engine,
):
    """Build the engine dispatcher for worker task execution."""
    from src.engine.router.engine_dispatcher import EngineDispatcher

    return EngineDispatcher(
        llm_client=llm_client,
        tool_executor=tool_executor,
        mcp_server=mcp_server,
        memory_flush_callback=memory_flush_callback,
        enhanced_planning_executor=enhanced_planning_executor,
        state_checkpointer=state_checkpointer,
        langgraph_engine=langgraph_engine,
    )


def build_task_store(workspace_root):
    """Build the task store used by TaskRunner."""
    from src.worker.task import TaskStore

    return TaskStore(workspace_root)


def build_task_runner(
    *,
    engine_dispatcher,
    task_store,
    post_run_handler,
    error_feedback_handler,
    state_checkpointer,
    tool_pipeline=None,
):
    """Build TaskRunner with lifecycle hooks attached."""
    from src.worker.task_runner import TaskRunner

    return TaskRunner(
        engine_dispatcher=engine_dispatcher,
        task_store=task_store,
        post_run_handler=post_run_handler,
        error_feedback_handler=error_feedback_handler,
        state_checkpointer=state_checkpointer,
        tool_pipeline=tool_pipeline,
    )


def build_worker_router(
    *,
    worker_registry,
    tenant_loader,
    task_runner,
    mcp_server,
    workspace_root,
    subagent_executor,
    memory_orchestrator,
    execution_scope_provider,
):
    """Build the WorkerRouter once TaskRunner is available."""
    from src.worker.router import WorkerRouter

    return WorkerRouter(
        worker_registry=worker_registry,
        tenant_loader=tenant_loader,
        task_runner=task_runner,
        mcp_server=mcp_server,
        workspace_root=workspace_root,
        subagent_executor=subagent_executor,
        memory_orchestrator=memory_orchestrator,
        execution_scope_provider=execution_scope_provider,
    )


def build_llm_client(context):
    """Build the main LLM client, preferring LiteLLM Router."""
    from src.common.logger import get_logger
    from src.services.llm.config_source import (
        MissingInjectedConfigError,
        build_litellm_config_source,
    )
    from src.services.llm.litellm_router import LiteLLMRouter
    from src.services.llm.model_tiers import ModelTier
    from src.services.llm.router_adapter import LiteLLMRouterAdapter
    from src.services.llm.routing_policy import TableRoutingPolicy
    from src.services.llm.testing import SmokeStubProvider

    logger = get_logger()
    router = context.get_state("litellm_router") if context is not None else None
    settings = getattr(context, "settings", None)
    if bool(getattr(settings, "community_smoke_profile", False)):
        logger.info("[LLM] community_smoke_profile enabled, using smoke stub provider")
        return SmokeStubProvider()
    config_manager = getattr(router, "config_manager", None) if router is not None else None
    needs_config_source = (
        config_manager is None
        or not hasattr(config_manager, "get_default_tier")
    )
    if settings is not None and needs_config_source:
        try:
            config_manager = build_litellm_config_source(settings)
        except MissingInjectedConfigError:
            raise
        except (OSError, ValueError) as exc:
            logger.warning(
                "[LLM] failed to load LiteLLM config for routing: %s",
                exc,
            )
    if router is not None:
        default_tier = _resolve_router_default_tier(config_manager)
        if default_tier is not None:
            return LiteLLMRouterAdapter(
                router,
                policy=TableRoutingPolicy(),
                default_tier=default_tier,
            )
        logger.warning(
            "[LLM] litellm_router present but no valid default_tier could be resolved "
            "from litellm.json; refusing implicit standard fallback"
        )
    if config_manager is not None:
        try:
            logger.warning(
                "[LLM] litellm_router missing from context, constructing local LiteLLMRouter "
                "from litellm config"
            )
            local_router = LiteLLMRouter(
                config_manager=config_manager,
                enable_fallback=True,
                enable_caching=False,
            )
            return LiteLLMRouterAdapter(
                local_router,
                policy=TableRoutingPolicy(),
                default_tier=_resolve_router_default_tier(config_manager),
            )
        except Exception as exc:
            logger.warning(
                "[LLM] failed to construct local LiteLLMRouter from LiteLLM config: %s",
                exc,
            )
    logger.warning(
        "[LLM] litellm_router not available, using unavailable fallback client; "
        "direct provider invocation disabled"
    )
    return build_unavailable_llm_client(
        settings if config_manager is not None else None,
        config_manager=config_manager,
    )


def _resolve_router_default_tier(config_manager):
    """Resolve a valid default tier from config manager without implicit fallback."""
    from src.services.llm.model_tiers import ModelTier

    if config_manager is None or not hasattr(config_manager, "get_default_tier"):
        return None
    try:
        raw_tier = config_manager.get_default_tier()
    except Exception:
        return None
    if not ModelTier.is_valid(raw_tier):
        return None
    return ModelTier(str(raw_tier).strip().lower())


from src.runtime.bootstrap_builders import (
    build_langgraph_stack,
    build_unavailable_llm_client,
    build_planning_stack,
    build_tool_executor,
)
from src.runtime.task_hooks import (
    build_error_feedback_handler,
    build_memory_flush_callback,
    build_post_run_handler,
)
