"""
Bootstrap module for coordinating application startup.

Provides the BootstrapOrchestrator and initializers for
topology-sorted initialization of subsystems.

Bootstrap chain (topology sorted):
    logging → events(25) → llm → memory → mcp → tool_discovery → skills → workers → api_wiring(memory_orchestrator) → scheduler(110) → conversation(120) → platforms(125) → contacts(128) → channels → integrations(130) → sensors(132) → heartbeat(135)
"""

import warnings

from src.common.paths import default_workspace_root

from .orchestrator import BootstrapOrchestrator
from .base import Initializer
from .context import BootstrapContext

_INITIALIZER_EXPORTS = {
    "LoggingInitializer": ".logging_init",
    "LLMInitializer": ".llm_init",
    "MemoryInitializer": ".memory_init",
    "MCPInitializer": ".mcp_init",
    "ToolDiscoveryInitializer": ".tool_discovery_init",
    "SkillInitializer": ".skill_init",
    "WorkerInitializer": ".worker_init",
    "LifecycleInitializer": ".lifecycle_init",
    "ApiWiringInitializer": ".api_wiring_init",
    "EventBusInitializer": ".event_bus_init",
    "SchedulerInitializer": ".scheduler_init",
    "IntegrationInitializer": ".integration_init",
    "ConversationInitializer": ".conversation_init",
    "PlatformInitializer": ".platform_init",
    "ContactInitializer": ".contact_init",
    "HeartbeatInitializer": ".heartbeat_init",
    "SensorInitializer": ".sensor_init",
    "ChannelInitializer": ".channel_init",
}


def create_orchestrator(tenant_id: str = "demo") -> BootstrapOrchestrator:
    """
    Create and configure the bootstrap orchestrator with all initializers.

    Bootstrap chain:
        logging → llm → memory → mcp → tool_discovery → skills → workers → api_wiring

    Args:
        tenant_id: Default tenant to load at startup.

    Returns:
        Configured BootstrapOrchestrator
    """
    from .api_wiring_init import ApiWiringInitializer
    from .channel_init import ChannelInitializer
    from .contact_init import ContactInitializer
    from .conversation_init import ConversationInitializer
    from .event_bus_init import EventBusInitializer
    from .heartbeat_init import HeartbeatInitializer
    from .integration_init import IntegrationInitializer
    from .lifecycle_init import LifecycleInitializer
    from .llm_init import LLMInitializer
    from .logging_init import LoggingInitializer
    from .mcp_init import MCPInitializer
    from .memory_init import MemoryInitializer
    from .platform_init import PlatformInitializer
    from .scheduler_init import SchedulerInitializer
    from .sensor_init import SensorInitializer
    from .skill_init import SkillInitializer
    from .tool_discovery_init import ToolDiscoveryInitializer
    from .worker_init import WorkerInitializer

    orchestrator = BootstrapOrchestrator()

    # Set initial context state
    orchestrator.set_initial_state("tenant_id", tenant_id)
    orchestrator.set_initial_state("workspace_root", default_workspace_root())

    # Phase 1: Core infrastructure
    orchestrator.register(LoggingInitializer())
    orchestrator.register(LLMInitializer())
    orchestrator.register(MemoryInitializer())

    # Phase 2: Tool layer
    orchestrator.register(MCPInitializer())
    orchestrator.register(ToolDiscoveryInitializer())

    # Phase 3: Skill system
    orchestrator.register(SkillInitializer())

    # Phase 5: Worker layer
    orchestrator.register(WorkerInitializer())
    orchestrator.register(LifecycleInitializer())

    # Final: API wiring (creates WorkerRouter + EngineDispatcher)
    orchestrator.register(ApiWiringInitializer())

    # Phase 7c: EventBus (priority=25, after logging)
    orchestrator.register(EventBusInitializer())

    # Phase 7c: Scheduler (priority=110, after api_wiring)
    orchestrator.register(SchedulerInitializer())

    # Platform services and contact registries
    orchestrator.register(PlatformInitializer())
    orchestrator.register(ContactInitializer())

    # Phase 7f: Integrations (priority=130, after scheduler)
    orchestrator.register(IntegrationInitializer())

    # Phase 7g: Conversation (priority=120, after api_wiring and events)
    orchestrator.register(ConversationInitializer())

    # Phase 9a: Sensor framework bootstrap
    orchestrator.register(SensorInitializer())

    # Phase 10: IM channel adapters
    orchestrator.register(ChannelInitializer())

    # Phase 8: Heartbeat cognitive loop (priority=135)
    orchestrator.register(HeartbeatInitializer())

    return orchestrator


_COMPAT_EXPORTS = {
    "_RouterAdapter",
    "_build_direct_llm_client",
    "_build_unavailable_llm_client",
    "_build_enhanced_planning_executor",
    "_build_error_feedback_handler",
    "_build_memory_flush_callback",
    "_build_planning_stack",
    "_build_post_run_handler",
    "_build_subagent_executor",
    "_build_tool_executor",
    "_build_tool_pipeline",
    "_parse_tool_args",
}


def __getattr__(name: str):
    if name in _INITIALIZER_EXPORTS:
        import importlib

        module = importlib.import_module(_INITIALIZER_EXPORTS[name], __name__)
        return getattr(module, name)
    if name in _COMPAT_EXPORTS:
        if name == "_build_direct_llm_client":
            warnings.warn(
                "_build_direct_llm_client is deprecated; use _build_unavailable_llm_client",
                DeprecationWarning,
                stacklevel=2,
            )
        from . import compat as _compat

        return getattr(_compat, name)
    raise AttributeError(name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | _COMPAT_EXPORTS | set(_INITIALIZER_EXPORTS))


__all__ = [
    "BootstrapOrchestrator",
    "Initializer",
    "BootstrapContext",
    "create_orchestrator",
    "LoggingInitializer",
    "LLMInitializer",
    "MemoryInitializer",
    "MCPInitializer",
    "ToolDiscoveryInitializer",
    "SkillInitializer",
    "WorkerInitializer",
    "LifecycleInitializer",
    "ApiWiringInitializer",
    "EventBusInitializer",
    "SchedulerInitializer",
    "IntegrationInitializer",
    "ConversationInitializer",
    "SensorInitializer",
    "HeartbeatInitializer",
]
