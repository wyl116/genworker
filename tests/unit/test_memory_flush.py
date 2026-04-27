# edition: baseline
import pytest

from src.context.compaction.memory_flush import flush_memory_before_compaction


class _Resp:
    def __init__(self, content: str):
        self.content = content


class _LLM:
    def __init__(self, content: str, fail: bool = False):
        self._content = content
        self._fail = fail

    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
        if self._fail:
            raise RuntimeError("llm fail")
        return _Resp(self._content)


@pytest.mark.asyncio
async def test_memory_flush_extracts_safe_artifacts():
    captured = []

    async def _callback(payload):
        captured.append(payload)

    await flush_memory_before_compaction(
        messages=({"role": "user", "content": "we found a pattern"},),
        llm_client=_LLM('{"episodes":["useful insight"],"rule_candidates":["Always validate data"]}'),
        learning_callback=_callback,
    )

    assert captured == [{"episodes": ("useful insight",), "rule_candidates": ("Always validate data",)}]


@pytest.mark.asyncio
async def test_memory_flush_empty_result():
    captured = []

    async def _callback(payload):
        captured.append(payload)

    await flush_memory_before_compaction(
        messages=({"role": "user", "content": "nothing"},),
        llm_client=_LLM('{"episodes":[],"rule_candidates":[]}'),
        learning_callback=_callback,
    )
    assert captured == [{"episodes": (), "rule_candidates": ()}]


@pytest.mark.asyncio
async def test_memory_flush_failure_is_silent():
    captured = []

    async def _callback(payload):
        captured.append(payload)

    await flush_memory_before_compaction(
        messages=({"role": "user", "content": "x"},),
        llm_client=_LLM("", fail=True),
        learning_callback=_callback,
    )
    assert captured == []
