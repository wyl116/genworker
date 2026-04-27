"""IM channel adapters."""
import importlib

_EXPORTS = {
    "DingTalkIMAdapter": ".dingtalk_adapter",
    "EmailIMAdapter": ".email_adapter",
    "FeishuIMAdapter": ".feishu_adapter",
    "SlackIMAdapter": ".slack_adapter",
    "WeComIMAdapter": ".wecom_adapter",
}


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module = importlib.import_module(_EXPORTS[name], __name__)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))

__all__ = ["DingTalkIMAdapter", "EmailIMAdapter", "FeishuIMAdapter", "SlackIMAdapter", "WeComIMAdapter"]
