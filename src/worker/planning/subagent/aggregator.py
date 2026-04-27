"""
SubAgent result aggregator and DAG topological sort.

Provides:
- topological_sort_to_layers: Kahn's algorithm for DAG layering
- aggregate_results: merge multiple SubAgentResults into AggregatedResult
"""
from __future__ import annotations

from collections import defaultdict

from src.worker.planning.models import PlanningError, SubGoal

from .models import AggregatedResult, SubAgentResult


def topological_sort_to_layers(
    sub_goals: tuple[SubGoal, ...],
) -> tuple[tuple[str, ...], ...]:
    """
    Perform topological sort using Kahn's algorithm, returning layers.

    Each layer contains SubGoal IDs that can execute in parallel.
    Layers must be executed sequentially (layer N depends on layer N-1).

    Raises PlanningError if a cycle is detected.
    """
    if not sub_goals:
        return ()

    # Build adjacency list and in-degree map
    all_ids = {sg.id for sg in sub_goals}
    in_degree: dict[str, int] = {sg.id: 0 for sg in sub_goals}
    dependents: dict[str, list[str]] = defaultdict(list)

    for sg in sub_goals:
        for dep in sg.depends_on:
            if dep in all_ids:
                in_degree[sg.id] += 1
                dependents[dep].append(sg.id)

    # Kahn's algorithm with layer grouping
    layers: list[tuple[str, ...]] = []
    current_layer = [sg_id for sg_id, deg in in_degree.items() if deg == 0]
    processed_count = 0

    while current_layer:
        layers.append(tuple(sorted(current_layer)))
        processed_count += len(current_layer)

        next_layer: list[str] = []
        for sg_id in current_layer:
            for dependent in dependents[sg_id]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    next_layer.append(dependent)

        current_layer = next_layer

    # Cycle detection: if not all nodes processed, there is a cycle
    if processed_count < len(all_ids):
        unprocessed = sorted(
            sg_id for sg_id in all_ids
            if in_degree[sg_id] > 0
        )
        raise PlanningError(
            f"SubGoal 依赖存在循环: {unprocessed}"
        )

    return tuple(layers)


def aggregate_results(
    sub_results: tuple[SubAgentResult, ...],
) -> AggregatedResult:
    """
    Merge multiple SubAgentResults into a single AggregatedResult.

    Combines content from successful results, counts successes/failures.
    """
    success_count = sum(
        1 for r in sub_results if r.status == "success"
    )
    failure_count = sum(
        1 for r in sub_results if r.status != "success"
    )

    content_parts = [
        r.content for r in sub_results
        if r.status == "success" and r.content
    ]
    combined = "\n\n".join(content_parts)

    return AggregatedResult(
        sub_results=sub_results,
        success_count=success_count,
        failure_count=failure_count,
        combined_content=combined,
    )
