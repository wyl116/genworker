"""
SubAgent subsystem - parallel execution of independent SubGoals.

Provides:
- SubAgentExecutor: spawn, collect, and cancel SubAgents
- aggregate_results: merge multiple SubAgent results
- topological_sort_to_layers: DAG layering for parallel execution
"""
