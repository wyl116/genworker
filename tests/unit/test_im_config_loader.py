# edition: baseline
from __future__ import annotations

import json
from pathlib import Path

from src.api.im_config_loader import load_worker_im_config, write_worker_im_config


def _write_worker_files(worker_dir: Path) -> None:
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "PERSONA.md").write_text(
        (
            "---\n"
            "identity:\n"
            "  worker_id: analyst-01\n"
            "channels:\n"
            "  - type: slack\n"
            "    connection_mode: socket_mode\n"
            "    chat_ids:\n"
            "      - C123\n"
            "---\n"
            "Body text.\n"
        ),
        encoding="utf-8",
    )
    (worker_dir / "CHANNEL_CREDENTIALS.json").write_text(
        json.dumps(
            {
                "slack": {
                    "bot_token": "xoxb-secret",
                    "app_token": "xapp-secret",
                    "team_id": "T123",
                }
            }
        ),
        encoding="utf-8",
    )


def test_load_worker_im_config_masks_secrets(tmp_path: Path) -> None:
    worker_dir = tmp_path / "tenants" / "demo" / "workers" / "analyst-01"
    _write_worker_files(worker_dir)

    payload = load_worker_im_config(
        workspace_root=tmp_path,
        tenant_id="demo",
        worker_id="analyst-01",
    )

    assert payload["persona"]["channels"][0]["type"] == "slack"
    assert payload["masked_credentials"]["slack"]["bot_token"] == "xoxb****"
    assert payload["masked_credentials"]["slack"]["team_id"] == "T123"


def test_write_worker_im_config_preserves_body_and_updates_channels(tmp_path: Path) -> None:
    worker_dir = tmp_path / "tenants" / "demo" / "workers" / "analyst-01"
    _write_worker_files(worker_dir)

    result = write_worker_im_config(
        workspace_root=tmp_path,
        tenant_id="demo",
        worker_id="analyst-01",
        channels=[
            {
                "type": "feishu",
                "connection_mode": "websocket",
                "chat_ids": ["oc_demo"],
                "reply_mode": "complete",
                "features": {"mention_required": True},
            }
        ],
        credentials={
            "feishu": {
                "app_id": "cli_demo",
                "app_secret": "secret",
            }
        },
    )

    text = (worker_dir / "PERSONA.md").read_text(encoding="utf-8")
    credentials = json.loads((worker_dir / "CHANNEL_CREDENTIALS.json").read_text(encoding="utf-8"))
    assert result["persona_written"] is True
    assert "Body text." in text
    assert "type: feishu" in text
    assert credentials["feishu"]["app_id"] == "cli_demo"
    assert credentials["slack"]["team_id"] == "T123"
