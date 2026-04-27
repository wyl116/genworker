"""
Worker planning subsystem - goal decomposition, strategy selection, and reflection.

Provides:
- Decomposer: breaks tasks into SubGoals with dependency DAG
- StrategySelector: matches SubGoals to Skills
- Reflector: evaluates completeness and proposes additional SubGoals
- EnhancedPlanningExecutor: orchestrates the full planning loop
- SubAgent: parallel execution of independent SubGoals
"""
