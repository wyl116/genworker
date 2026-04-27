"""Memory package exports."""
import importlib

_EXPORTS = {
    "MemoryOrchestrator": ".orchestrator",
    "MemoryQueryResult": ".orchestrator",
    "MemoryBudgetConfig": ".orchestrator",
    "MemoryWriteEvent": ".write_models",
    "MemoryProvider": ".provider",
    "MemoryRecallResult": ".provider",
    "SemanticMemoryProvider": ".provider",
    "EpisodicMemoryProvider": ".provider",
    "PreferenceMemoryProvider": ".provider",
    "EpisodeWritePayload": ".write_models",
    "SemanticFactWritePayload": ".write_models",
    "PreferenceWritePayload": ".write_models",
    "DecisionWritePayload": ".write_models",
}


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module = importlib.import_module(_EXPORTS[name], __name__)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))

__all__ = [
    "MemoryOrchestrator",
    "MemoryQueryResult",
    "MemoryWriteEvent",
    "MemoryBudgetConfig",
    "MemoryProvider",
    "MemoryRecallResult",
    "EpisodeWritePayload",
    "SemanticFactWritePayload",
    "PreferenceWritePayload",
    "DecisionWritePayload",
    "SemanticMemoryProvider",
    "EpisodicMemoryProvider",
    "PreferenceMemoryProvider",
]
