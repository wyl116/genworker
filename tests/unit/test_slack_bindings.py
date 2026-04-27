# edition: baseline
from __future__ import annotations

from types import SimpleNamespace

from src.channels.bindings import build_worker_bindings


class _Entry:
    def __init__(self, worker_id: str, channels: list[dict]) -> None:
        self.worker = SimpleNamespace(worker_id=worker_id, channels=channels)


def test_build_worker_bindings_allows_slack_without_chat_ids() -> None:
    entry = _Entry(
        "worker-1",
        [{
            "type": "slack",
            "connection_mode": "webhook",
            "reply_mode": "complete",
        }],
    )

    bindings = build_worker_bindings(entry, tenant_id="demo")

    assert len(bindings) == 1
    assert bindings[0].channel_type == "slack"
    assert bindings[0].chat_ids == ()
