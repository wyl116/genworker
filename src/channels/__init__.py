"""Bidirectional IM channel subsystem."""
import importlib

_EXPORTS = {
    "ChannelManager": ".manager",
    "Attachment": ".models",
    "ChannelBinding": ".models",
    "ChannelInboundMessage": ".models",
    "Mention": ".models",
    "ReplyContent": ".models",
    "StreamChunk": ".models",
    "build_channel_binding": ".models",
    "ChannelAdapter": ".outbound",
    "ChannelSendError": ".outbound",
    "DingTalkChannelAdapter": ".outbound",
    "DirectEmailAdapter": ".outbound",
    "EmailChannelAdapter": ".outbound",
    "FeishuChannelAdapter": ".outbound",
    "MultiChannelFallback": ".outbound",
    "ReliableChannelAdapter": ".outbound",
    "WeComChannelAdapter": ".outbound",
    "ChannelMessage": ".outbound_types",
    "ChannelPriority": ".outbound_types",
    "RetryConfig": ".outbound_types",
    "SenderScope": ".outbound_types",
    "IMChannelAdapter": ".protocol",
    "IMChannelRegistry": ".registry",
    "ChannelMessageRouter": ".router",
    "EmailIMAdapter": ".adapters",
    "build_worker_bindings": ".bindings",
}


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module = importlib.import_module(_EXPORTS[name], __name__)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))

__all__ = [
    "Attachment",
    "ChannelBinding",
    "ChannelInboundMessage",
    "ChannelManager",
    "ChannelMessage",
    "ChannelMessageRouter",
    "ChannelPriority",
    "ChannelSendError",
    "ChannelAdapter",
    "DingTalkChannelAdapter",
    "DirectEmailAdapter",
    "EmailIMAdapter",
    "EmailChannelAdapter",
    "FeishuChannelAdapter",
    "IMChannelAdapter",
    "IMChannelRegistry",
    "Mention",
    "MultiChannelFallback",
    "ReliableChannelAdapter",
    "ReplyContent",
    "RetryConfig",
    "SenderScope",
    "StreamChunk",
    "WeComChannelAdapter",
    "build_channel_binding",
    "build_worker_bindings",
]
