# edition: baseline
from __future__ import annotations

import pytest

from src.common.settings import get_settings
from src.worker.scripts.models import InlineScript


def test_inline_script_accepts_source_at_size_limit(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "code_exec_inline_size_limit_bytes", 8, raising=False)

    script = InlineScript(source="12345678")

    assert script.source == "12345678"


def test_inline_script_rejects_source_over_size_limit(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "code_exec_inline_size_limit_bytes", 8, raising=False)

    with pytest.raises(ValueError, match="exceeds 8B limit"):
        InlineScript(source="123456789")
