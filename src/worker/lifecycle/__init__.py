"""Lifecycle intelligence helpers for task/goal/duty/skill linkage."""
import importlib

_EXPORTS = {
    "DutySkillDetector": ".duty_skill_detector",
    "run_duty_skill_detection": ".duty_skill_detector",
    "FeedbackStore": ".feedback_store",
    "GoalLockRegistry": ".goal_projector",
    "GoalProjectResult": ".goal_projector",
    "project_task_outcome_to_goal": ".goal_projector",
    "FeedbackRecord": ".models",
    "SuggestionRecord": ".models",
    "build_skill_from_payload": ".skill_builder",
    "write_skill_md": ".skill_builder",
    "SuggestionStore": ".suggestion_store",
}


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module = importlib.import_module(_EXPORTS[name], __name__)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))

__all__ = [
    "DutySkillDetector",
    "run_duty_skill_detection",
    "FeedbackRecord",
    "FeedbackStore",
    "GoalLockRegistry",
    "GoalProjectResult",
    "SuggestionRecord",
    "SuggestionStore",
    "build_skill_from_payload",
    "project_task_outcome_to_goal",
    "write_skill_md",
]
