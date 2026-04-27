"""Heartbeat components."""
import importlib

_EXPORTS = {
    "AttentionLedger": ".ledger",
    "HeartbeatRunner": ".runner",
    "HeartbeatAction": ".strategy",
    "HeartbeatStrategy": ".strategy",
    "HeartbeatStrategyConfig": ".strategy",
}


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module = importlib.import_module(_EXPORTS[name], __name__)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))

__all__ = [
    "AttentionLedger",
    "HeartbeatRunner",
    "HeartbeatAction",
    "HeartbeatStrategy",
    "HeartbeatStrategyConfig",
]
