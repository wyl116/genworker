"""Filesystem-backed helpers for worker IM config routes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import frontmatter

from src.api.im_config_validators import mask_credentials

_PERSONA_FILENAME = "PERSONA.md"
_CREDENTIALS_FILENAME = "CHANNEL_CREDENTIALS.json"


def resolve_worker_im_paths(
    workspace_root: str | Path,
    tenant_id: str,
    worker_id: str,
) -> tuple[Path, Path]:
    worker_dir = (
        Path(workspace_root)
        / "tenants"
        / tenant_id
        / "workers"
        / worker_id
    )
    return worker_dir / _PERSONA_FILENAME, worker_dir / _CREDENTIALS_FILENAME


def load_worker_im_config(
    *,
    workspace_root: str | Path,
    tenant_id: str,
    worker_id: str,
) -> dict[str, Any]:
    """Load worker persona channels and credentials from filesystem."""
    persona_path, credentials_path = resolve_worker_im_paths(
        workspace_root,
        tenant_id,
        worker_id,
    )
    if not persona_path.is_file():
        raise FileNotFoundError(f"PERSONA.md not found for worker '{worker_id}'")

    post = frontmatter.loads(persona_path.read_text(encoding="utf-8"))
    channels = post.metadata.get("channels", [])
    if not isinstance(channels, list):
        channels = []
    credentials = _read_credentials(credentials_path)
    return {
        "worker_id": worker_id,
        "persona": {
            "channels": [dict(item) for item in channels if isinstance(item, dict)],
        },
        "credentials": credentials,
        "masked_credentials": mask_credentials(credentials),
        "persona_path": persona_path,
        "credentials_path": credentials_path,
    }


def write_worker_im_config(
    *,
    workspace_root: str | Path,
    tenant_id: str,
    worker_id: str,
    channels: list[dict[str, Any]],
    credentials: dict[str, Any],
) -> dict[str, bool]:
    """Persist channels into PERSONA.md and credentials into JSON."""
    persona_path, credentials_path = resolve_worker_im_paths(
        workspace_root,
        tenant_id,
        worker_id,
    )
    if not persona_path.is_file():
        raise FileNotFoundError(f"PERSONA.md not found for worker '{worker_id}'")

    post = frontmatter.loads(persona_path.read_text(encoding="utf-8"))
    metadata = dict(post.metadata)
    metadata["channels"] = [dict(item) for item in channels]
    rendered = frontmatter.dumps(frontmatter.Post(post.content, **metadata))
    if not rendered.endswith("\n"):
        rendered += "\n"
    persona_path.write_text(rendered, encoding="utf-8")

    existing_credentials = _read_credentials(credentials_path)
    next_credentials = dict(existing_credentials)
    for platform, raw in credentials.items():
        if raw is None:
            next_credentials.pop(platform, None)
            continue
        next_credentials[platform] = dict(raw)
    if next_credentials:
        credentials_path.write_text(
            json.dumps(next_credentials, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    elif credentials_path.exists():
        credentials_path.unlink()

    return {
        "persona_written": True,
        "credentials_written": True,
    }


def _read_credentials(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return {
        str(key): dict(value)
        for key, value in raw.items()
        if isinstance(value, dict)
    }
