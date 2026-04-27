"""Bootstrap initializer for lifecycle intelligence infrastructure."""
from __future__ import annotations

from pathlib import Path

from src.common.logger import get_logger
from src.worker.lifecycle.feedback_store import FeedbackStore
from src.worker.lifecycle.goal_projector import GoalLockRegistry
from src.worker.lifecycle.services import LifecycleServices
from src.worker.lifecycle.suggestion_store import SuggestionStore

from .base import Initializer

logger = get_logger()


class LifecycleInitializer(Initializer):
    """Initialize file-backed lifecycle stores and registries."""

    @property
    def name(self) -> str:
        return "lifecycle"

    @property
    def depends_on(self) -> list[str]:
        return ["workers"]

    @property
    def priority(self) -> int:
        return 95

    async def initialize(self, context) -> bool:
        workspace_root = Path(context.get_state("workspace_root", "workspace"))
        suggestion_store = SuggestionStore(workspace_root)
        feedback_store = FeedbackStore(workspace_root)
        goal_lock_registry = GoalLockRegistry()
        context.set_state("suggestion_store", suggestion_store)
        context.set_state("feedback_store", feedback_store)
        context.set_state("goal_lock_registry", goal_lock_registry)
        context.set_state(
            "lifecycle_services",
            LifecycleServices(
                workspace_root=workspace_root,
                suggestion_store=suggestion_store,
                feedback_store=feedback_store,
                goal_lock_registry=goal_lock_registry,
            ),
        )
        logger.info("[LifecycleInit] Lifecycle stores initialized")
        return True

    async def cleanup(self) -> None:
        return None
