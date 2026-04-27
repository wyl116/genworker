# edition: baseline
from dataclasses import FrozenInstanceError

import pytest

from src.services.llm.intent import DEFAULT_INTENT, LLMCallIntent, Purpose


def test_default_intent_is_generate():
    assert DEFAULT_INTENT.purpose is Purpose.GENERATE
    assert DEFAULT_INTENT.requires_tools is False


def test_intent_is_frozen():
    intent = LLMCallIntent(purpose=Purpose.PLAN)
    with pytest.raises(FrozenInstanceError):
        intent.purpose = Purpose.CHAT_TURN  # type: ignore[misc]
