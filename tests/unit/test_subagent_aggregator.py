# edition: baseline
"""Tests for SubAgent aggregator and topological sort."""
from __future__ import annotations

import pytest

from src.worker.planning.models import PlanningError, SubGoal
from src.worker.planning.subagent.aggregator import (
    aggregate_results,
    topological_sort_to_layers,
)
from src.worker.planning.subagent.models import (
    AggregatedResult,
    SubAgentResult,
    SubAgentUsage,
)


# ---------------------------------------------------------------------------
# Tests: topological_sort_to_layers
# ---------------------------------------------------------------------------

def test_topo_sort_empty():
    """Empty input returns empty layers."""
    assert topological_sort_to_layers(()) == ()


def test_topo_sort_single_goal():
    """Single goal produces one layer."""
    goals = (SubGoal(id="sg-1", description="only one"),)
    layers = topological_sort_to_layers(goals)

    assert layers == (("sg-1",),)


def test_topo_sort_independent_goals():
    """Independent goals are in the same layer."""
    goals = (
        SubGoal(id="sg-1", description="A"),
        SubGoal(id="sg-2", description="B"),
        SubGoal(id="sg-3", description="C"),
    )
    layers = topological_sort_to_layers(goals)

    assert len(layers) == 1
    assert set(layers[0]) == {"sg-1", "sg-2", "sg-3"}


def test_topo_sort_linear_chain():
    """Linear dependency chain produces one goal per layer."""
    goals = (
        SubGoal(id="sg-1", description="First"),
        SubGoal(id="sg-2", description="Second", depends_on=("sg-1",)),
        SubGoal(id="sg-3", description="Third", depends_on=("sg-2",)),
    )
    layers = topological_sort_to_layers(goals)

    assert len(layers) == 3
    assert layers[0] == ("sg-1",)
    assert layers[1] == ("sg-2",)
    assert layers[2] == ("sg-3",)


def test_topo_sort_diamond_dependency():
    """Diamond pattern: A -> B, A -> C, B+C -> D."""
    goals = (
        SubGoal(id="A", description="root"),
        SubGoal(id="B", description="left", depends_on=("A",)),
        SubGoal(id="C", description="right", depends_on=("A",)),
        SubGoal(id="D", description="merge", depends_on=("B", "C")),
    )
    layers = topological_sort_to_layers(goals)

    assert len(layers) == 3
    assert layers[0] == ("A",)
    assert set(layers[1]) == {"B", "C"}
    assert layers[2] == ("D",)


def test_topo_sort_mixed_dependencies():
    """Mix of dependent and independent goals."""
    goals = (
        SubGoal(id="sg-1", description="Independent"),
        SubGoal(id="sg-2", description="Also independent"),
        SubGoal(id="sg-3", description="Depends on sg-1", depends_on=("sg-1",)),
        SubGoal(id="sg-4", description="Depends on sg-2 and sg-3", depends_on=("sg-2", "sg-3")),
    )
    layers = topological_sort_to_layers(goals)

    # Layer 0: sg-1, sg-2
    assert set(layers[0]) == {"sg-1", "sg-2"}
    # Layer 1: sg-3
    assert layers[1] == ("sg-3",)
    # Layer 2: sg-4
    assert layers[2] == ("sg-4",)


def test_topo_sort_cyclic_dependency_raises():
    """Cyclic dependencies raise PlanningError."""
    goals = (
        SubGoal(id="sg-1", description="A", depends_on=("sg-2",)),
        SubGoal(id="sg-2", description="B", depends_on=("sg-1",)),
    )
    with pytest.raises(PlanningError, match="循环"):
        topological_sort_to_layers(goals)


def test_topo_sort_three_node_cycle_raises():
    """Three-node cycle raises PlanningError."""
    goals = (
        SubGoal(id="A", description="A", depends_on=("C",)),
        SubGoal(id="B", description="B", depends_on=("A",)),
        SubGoal(id="C", description="C", depends_on=("B",)),
    )
    with pytest.raises(PlanningError, match="循环"):
        topological_sort_to_layers(goals)


def test_topo_sort_ignores_unknown_dependencies():
    """Dependencies on IDs not in the goal set are ignored."""
    goals = (
        SubGoal(id="sg-1", description="A", depends_on=("unknown",)),
    )
    layers = topological_sort_to_layers(goals)

    assert layers == (("sg-1",),)


def test_topo_sort_layer_ids_are_sorted():
    """IDs within each layer are sorted for deterministic output."""
    goals = (
        SubGoal(id="z", description="Z"),
        SubGoal(id="a", description="A"),
        SubGoal(id="m", description="M"),
    )
    layers = topological_sort_to_layers(goals)

    assert layers[0] == ("a", "m", "z")


# ---------------------------------------------------------------------------
# Tests: aggregate_results
# ---------------------------------------------------------------------------

def test_aggregate_empty():
    """Empty results produce zero counts and empty content."""
    agg = aggregate_results(())

    assert agg.success_count == 0
    assert agg.failure_count == 0
    assert agg.combined_content == ""
    assert agg.sub_results == ()


def test_aggregate_all_success():
    """All successes produce correct counts and combined content."""
    results = (
        SubAgentResult(agent_id="a", sub_goal_id="sg-1", status="success", content="Alpha"),
        SubAgentResult(agent_id="b", sub_goal_id="sg-2", status="success", content="Beta"),
    )
    agg = aggregate_results(results)

    assert agg.success_count == 2
    assert agg.failure_count == 0
    assert "Alpha" in agg.combined_content
    assert "Beta" in agg.combined_content


def test_aggregate_mixed_results():
    """Mixed success and failure produces correct counts."""
    results = (
        SubAgentResult(agent_id="a", sub_goal_id="sg-1", status="success", content="Good"),
        SubAgentResult(agent_id="b", sub_goal_id="sg-2", status="failure", content="", error="oops"),
        SubAgentResult(agent_id="c", sub_goal_id="sg-3", status="timeout", content="", error="timeout"),
    )
    agg = aggregate_results(results)

    assert agg.success_count == 1
    assert agg.failure_count == 2
    assert "Good" in agg.combined_content
    # Failure content is excluded from combined
    assert "oops" not in agg.combined_content


def test_aggregate_preserves_sub_results():
    """Aggregation preserves all sub-results in order."""
    results = (
        SubAgentResult(agent_id="a", sub_goal_id="sg-1", status="success", content="A"),
        SubAgentResult(agent_id="b", sub_goal_id="sg-2", status="success", content="B"),
    )
    agg = aggregate_results(results)

    assert len(agg.sub_results) == 2
    assert agg.sub_results[0].agent_id == "a"
    assert agg.sub_results[1].agent_id == "b"


def test_aggregated_result_is_frozen():
    """AggregatedResult is immutable."""
    agg = AggregatedResult(
        sub_results=(),
        success_count=0,
        failure_count=0,
        combined_content="",
    )
    with pytest.raises(AttributeError):
        agg.success_count = 5  # type: ignore[misc]
