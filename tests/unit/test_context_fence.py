# edition: baseline
from src.common.context_fence import (
    fence_memory_context,
    fence_rules_context,
    fence_shared_rules_context,
)


def test_fence_rules_context_wraps_xml():
    result = fence_rules_context("[Learned Rules]\n- do x")
    assert "<learned-rules>" in result
    assert "行为规则" in result


def test_fence_memory_context_includes_source():
    result = fence_memory_context("history", source="episodic_memory")
    assert 'source="episodic_memory"' in result
    assert "<memory-context" in result


def test_fence_shared_rules_context_mentions_other_workers():
    result = fence_shared_rules_context("rule text")
    assert "<shared-rules>" in result
    assert "其他 Worker" in result
