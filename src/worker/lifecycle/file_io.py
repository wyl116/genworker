"""Lifecycle-local file I/O helpers."""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write text via same-directory temp file and atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    tmp_path.write_text(content, encoding=encoding)
    os.replace(tmp_path, path)
