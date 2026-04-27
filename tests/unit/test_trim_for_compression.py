# edition: baseline
from src.context.compaction.tool_trimmer import trim_for_compression


def test_short_result_preserved():
    messages = ({"role": "tool", "content": "short"},)

    result = trim_for_compression(messages, char_threshold=10)

    assert result == messages


def test_long_result_replaced_with_placeholder():
    messages = (
        {"role": "tool", "content": "x" * 50, "name": "search_tool"},
        {"role": "assistant", "content": "done"},
    )

    result = trim_for_compression(messages, char_threshold=10)

    assert result[0]["content"] == "[tool result: search_tool, 50 chars cleared]"
    assert result[1]["content"] == "done"
