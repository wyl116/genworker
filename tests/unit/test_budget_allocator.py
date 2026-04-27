# edition: baseline
"""Tests for budget_allocator - token budget distribution."""
import pytest

from src.context.budget_allocator import (
    allocate_budgets,
    compute_overflow,
    select_segments_to_compress,
    trim_segment_to_budget,
)
from src.context.models import ContextSegment, ContextWindowConfig


def _seg(name: str, tokens: int, priority: int = 50,
         max_tokens: int = 0, compressible: bool = True) -> ContextSegment:
    return ContextSegment(
        name=name,
        content="x" * (tokens * 3),
        token_count=tokens,
        priority=priority,
        max_tokens=max_tokens,
        compressible=compressible,
    )


class TestAllocateBudgets:
    def test_fixed_segments_keep_budget(self):
        segs = (
            _seg("identity", 100, priority=0, max_tokens=500),
            _seg("history", 5000, priority=60, max_tokens=0),
        )
        config = ContextWindowConfig()
        result = allocate_budgets(segs, config)
        assert result[0].max_tokens == 500

    def test_elastic_segments_get_remaining(self):
        segs = (
            _seg("identity", 100, priority=0, max_tokens=500),
            _seg("history", 5000, priority=60, max_tokens=0),
        )
        config = ContextWindowConfig()
        result = allocate_budgets(segs, config)
        effective = config.effective_window
        expected_elastic = effective - 500
        assert result[1].max_tokens == expected_elastic

    def test_multiple_elastic_split_equally(self):
        segs = (
            _seg("fixed", 100, priority=0, max_tokens=1000),
            _seg("elastic_a", 200, priority=60, max_tokens=0),
            _seg("elastic_b", 300, priority=70, max_tokens=0),
        )
        config = ContextWindowConfig()
        result = allocate_budgets(segs, config)
        remaining = config.effective_window - 1000
        per_elastic = remaining // 2
        assert result[1].max_tokens == per_elastic
        assert result[2].max_tokens == per_elastic

    def test_returns_new_tuples(self):
        segs = (
            _seg("a", 100, max_tokens=0),
        )
        config = ContextWindowConfig()
        result = allocate_budgets(segs, config)
        assert result is not segs
        assert result[0] is not segs[0]

    def test_all_fixed_no_elastic(self):
        segs = (
            _seg("a", 100, max_tokens=500),
            _seg("b", 200, max_tokens=1000),
        )
        config = ContextWindowConfig()
        result = allocate_budgets(segs, config)
        assert result[0].max_tokens == 500
        assert result[1].max_tokens == 1000


class TestTrimSegmentToBudget:
    def test_within_budget_unchanged(self):
        seg = _seg("test", 100, max_tokens=500)
        result = trim_segment_to_budget(seg)
        assert result.content == seg.content

    def test_non_compressible_unchanged(self):
        seg = _seg("identity", 1000, max_tokens=100, compressible=False)
        result = trim_segment_to_budget(seg)
        assert result.content == seg.content

    def test_zero_budget_unchanged(self):
        seg = _seg("test", 1000, max_tokens=0)
        result = trim_segment_to_budget(seg)
        assert result.content == seg.content

    def test_over_budget_trimmed(self):
        content = "a" * 10000
        seg = ContextSegment(
            name="test",
            content=content,
            token_count=3000,
            priority=50,
            max_tokens=100,
            compressible=True,
        )
        result = trim_segment_to_budget(seg)
        assert len(result.content) < len(content)
        assert result.token_count <= seg.token_count

    def test_returns_new_segment(self):
        content = "a" * 10000
        seg = ContextSegment(
            name="test",
            content=content,
            token_count=3000,
            priority=50,
            max_tokens=100,
            compressible=True,
        )
        result = trim_segment_to_budget(seg)
        assert result is not seg


class TestComputeOverflow:
    def test_no_overflow(self):
        segs = (_seg("a", 100), _seg("b", 200))
        assert compute_overflow(segs, 1000) == 0

    def test_exact_fit(self):
        segs = (_seg("a", 500), _seg("b", 500))
        assert compute_overflow(segs, 1000) == 0

    def test_overflow(self):
        segs = (_seg("a", 600), _seg("b", 600))
        assert compute_overflow(segs, 1000) == 200

    def test_empty_segments(self):
        assert compute_overflow((), 1000) == 0


class TestSelectSegmentsToCompress:
    def test_no_overflow_returns_empty(self):
        segs = (_seg("a", 100, priority=50),)
        assert select_segments_to_compress(segs, 0) == ()

    def test_selects_lowest_priority_first(self):
        segs = (
            _seg("high", 100, priority=10),
            _seg("medium", 200, priority=50),
            _seg("low", 300, priority=70),
        )
        result = select_segments_to_compress(segs, 200)
        assert result[0] == "low"

    def test_accumulates_until_overflow_covered(self):
        segs = (
            _seg("high", 100, priority=10),
            _seg("medium", 200, priority=50),
            _seg("low", 300, priority=70),
        )
        result = select_segments_to_compress(segs, 400)
        assert "low" in result
        assert "medium" in result

    def test_skips_non_compressible(self):
        segs = (
            _seg("fixed", 1000, priority=70, compressible=False),
            _seg("flex", 200, priority=50),
        )
        result = select_segments_to_compress(segs, 500)
        assert "fixed" not in result
        assert "flex" in result

    def test_skips_zero_token_segments(self):
        segs = (
            _seg("empty", 0, priority=70),
            _seg("full", 200, priority=50),
        )
        result = select_segments_to_compress(segs, 100)
        assert "empty" not in result
