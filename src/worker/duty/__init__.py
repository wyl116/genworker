"""
Duty subsystem - responsibility-based proactive task execution.

Provides:
- Duty: immutable duty definition model
- parse_duty: DUTY.md parser with validation
- TriggerManager: multi-source trigger management
- DutyExecutor: duty-to-task conversion via WorkerRouter
- ExecutionLog: append-only execution record storage
"""
import importlib

_EXPORTS = {
    "Duty": ".models",
    "DutyTrigger": ".models",
    "ExecutionPolicy": ".models",
    "EscalationPolicy": ".models",
    "DutyExecutionRecord": ".models",
    "parse_duty": ".parser",
    "TriggerManager": ".trigger_manager",
    "select_execution_depth": ".trigger_manager",
    "write_execution_record": ".execution_log",
    "load_recent_records": ".execution_log",
}


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module = importlib.import_module(_EXPORTS[name], __name__)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))

__all__ = [
    "Duty",
    "DutyTrigger",
    "ExecutionPolicy",
    "EscalationPolicy",
    "DutyExecutionRecord",
    "parse_duty",
    "TriggerManager",
    "select_execution_depth",
    "write_execution_record",
    "load_recent_records",
]
