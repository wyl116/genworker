"""Decision strategy for heartbeat inbox items."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from src.autonomy.inbox import InboxItem
from src.worker.models import WorkerHeartbeatConfig


@dataclass(frozen=True)
class HeartbeatAction:
    """Execution decision derived from one inbox item."""

    kind: str  # "summary" | "task" | "isolated"
    task_description: str = ""
    preferred_skill_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class HeartbeatStrategyConfig:
    """Configurable thresholds and action groups for heartbeat decisions."""

    goal_task_actions: frozenset[str] = field(
        default_factory=lambda: frozenset({"escalate", "recover", "investigate"})
    )
    goal_isolated_actions: frozenset[str] = field(
        default_factory=lambda: frozenset({"replan", "deep_review"})
    )
    goal_isolated_deviation_threshold: float = 0.9

    @classmethod
    def from_settings(cls, settings: Any | None) -> "HeartbeatStrategyConfig":
        """Build strategy config from application settings."""
        if settings is None:
            return cls()
        return cls(
            goal_task_actions=_csv_to_frozenset(
                getattr(
                    settings,
                    "heartbeat_goal_task_actions",
                    "escalate,recover,investigate",
                )
            ),
            goal_isolated_actions=_csv_to_frozenset(
                getattr(
                    settings,
                    "heartbeat_goal_isolated_actions",
                    "replan,deep_review",
                )
            ),
            goal_isolated_deviation_threshold=float(
                getattr(
                    settings,
                    "heartbeat_goal_isolated_deviation_threshold",
                    0.9,
                ) or 0.9
            ),
        )

    def with_worker_overrides(
        self,
        worker_config: WorkerHeartbeatConfig | None,
    ) -> "HeartbeatStrategyConfig":
        """Overlay worker-level heartbeat config on top of global defaults."""
        if worker_config is None:
            return self
        return HeartbeatStrategyConfig(
            goal_task_actions=(
                frozenset(worker_config.goal_task_actions)
                if worker_config.goal_task_actions
                else self.goal_task_actions
            ),
            goal_isolated_actions=(
                frozenset(worker_config.goal_isolated_actions)
                if worker_config.goal_isolated_actions
                else self.goal_isolated_actions
            ),
            goal_isolated_deviation_threshold=(
                float(worker_config.goal_isolated_deviation_threshold)
                if worker_config.goal_isolated_deviation_threshold is not None
                else self.goal_isolated_deviation_threshold
            ),
        )


class HeartbeatStrategy:
    """Map inbox facts to summary/task/isolated actions."""

    def __init__(
        self,
        config: HeartbeatStrategyConfig | None = None,
    ) -> None:
        self._config = config or HeartbeatStrategyConfig()

    def decide_action(self, item: InboxItem) -> HeartbeatAction:
        explicit_task = self.extract_task_description(item)
        run_mode = str(item.payload.get("run_mode", "")).strip().lower()
        if run_mode == "isolated":
            return HeartbeatAction(
                kind="isolated",
                task_description=explicit_task or self.build_default_task(item),
                preferred_skill_ids=self.extract_preferred_skill_ids(item),
            )
        if run_mode == "task":
            return HeartbeatAction(
                kind="task",
                task_description=explicit_task or self.build_default_task(item),
                preferred_skill_ids=self.extract_preferred_skill_ids(item),
            )

        if explicit_task:
            return HeartbeatAction(
                kind="task",
                task_description=explicit_task,
                preferred_skill_ids=self.extract_preferred_skill_ids(item),
            )

        if item.source_type == "goal_check":
            return self._decide_goal_check_action(item)

        if item.event_type == "external.email_received":
            return self._decide_email_action(item)

        if item.event_type == "external.feishu_doc_updated":
            return self._decide_feishu_action(item)

        return HeartbeatAction(kind="summary")

    def extract_task_description(self, item: InboxItem) -> str:
        """Read explicit task description fields from payload."""
        for field in ("task_description", "task", "prompt"):
            value = item.payload.get(field, "")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def extract_preferred_skill_ids(self, item: InboxItem) -> tuple[str, ...]:
        """Read soft-preferred skills from payload."""
        raw = (
            item.payload.get("preferred_skill_ids")
            or item.payload.get("skills")
            or ()
        )
        if isinstance(raw, str):
            raw = [raw]
        return tuple(
            str(skill_id).strip()
            for skill_id in raw
            if str(skill_id).strip()
        )

    def summarize_item(self, item: InboxItem) -> str:
        """Build a short summary for dedupe bookkeeping."""
        explicit_task = self.extract_task_description(item)
        if explicit_task:
            return explicit_task[:200]
        return (
            f"{item.source_type}:{item.event_type}:"
            f"{json.dumps(item.payload, ensure_ascii=False)[:160]}"
        )

    def build_default_task(self, item: InboxItem) -> str:
        """Generate a default task prompt from one inbox item."""
        if item.source_type == "goal_check":
            return self._build_goal_check_task(item)
        if item.event_type == "external.email_received":
            return self._build_email_followup_task(item)
        if item.event_type == "external.feishu_doc_updated":
            return self._build_feishu_review_task(item)
        return self.summarize_item(item)

    def _decide_goal_check_action(self, item: InboxItem) -> HeartbeatAction:
        recommended_action = str(
            item.payload.get("recommended_action", "")
        ).strip().lower()
        deviation_score = float(item.payload.get("deviation_score", 0.0) or 0.0)
        task = self._build_goal_check_task(item)
        if (
            recommended_action in self._config.goal_isolated_actions
            or deviation_score >= self._config.goal_isolated_deviation_threshold
        ):
            return HeartbeatAction(
                kind="isolated",
                task_description=task,
                preferred_skill_ids=self.extract_preferred_skill_ids(item),
            )
        if recommended_action in self._config.goal_task_actions:
            return HeartbeatAction(
                kind="task",
                task_description=task,
                preferred_skill_ids=self.extract_preferred_skill_ids(item),
            )
        return HeartbeatAction(kind="summary")

    def _decide_email_action(self, item: InboxItem) -> HeartbeatAction:
        if _as_bool(item.payload.get("requires_follow_up")):
            return HeartbeatAction(
                kind="task",
                task_description=self._build_email_followup_task(item),
                preferred_skill_ids=self.extract_preferred_skill_ids(item),
            )
        if _as_bool(item.payload.get("requires_deep_analysis")):
            return HeartbeatAction(
                kind="isolated",
                task_description=self._build_email_followup_task(item),
                preferred_skill_ids=self.extract_preferred_skill_ids(item),
            )
        return HeartbeatAction(kind="summary")

    def _decide_feishu_action(self, item: InboxItem) -> HeartbeatAction:
        if _as_bool(item.payload.get("requires_deep_analysis")):
            return HeartbeatAction(
                kind="isolated",
                task_description=self._build_feishu_review_task(item),
                preferred_skill_ids=self.extract_preferred_skill_ids(item),
            )
        if _as_bool(item.payload.get("requires_follow_up")):
            return HeartbeatAction(
                kind="task",
                task_description=self._build_feishu_review_task(item),
                preferred_skill_ids=self.extract_preferred_skill_ids(item),
            )
        return HeartbeatAction(kind="summary")

    def _build_goal_check_task(self, item: InboxItem) -> str:
        goal_title = str(item.payload.get("goal_title", item.payload.get("goal_id", "")))
        recommended_action = str(item.payload.get("recommended_action", "investigate"))
        deviation_score = item.payload.get("deviation_score", 0.0)
        return "\n".join(
            (
                f"[Goal Health Follow-up] {goal_title}",
                f"Recommended Action: {recommended_action}",
                f"Deviation Score: {deviation_score}",
                "请评估当前目标偏差，给出下一步行动，并说明是否需要通知相关方。",
            )
        )

    def _build_email_followup_task(self, item: InboxItem) -> str:
        subject = str(item.payload.get("subject", "未命名邮件"))
        sender = str(item.payload.get("from", ""))
        content = str(item.payload.get("content", ""))
        return "\n".join(
            (
                f"[Email Follow-up] {subject}",
                f"Sender: {sender}",
                f"Content Summary: {content[:500]}",
                "请判断这封邮件是否需要后续行动，并产出可执行的下一步建议。",
            )
        )

    def _build_feishu_review_task(self, item: InboxItem) -> str:
        path = str(item.payload.get("path", ""))
        modified_at = str(item.payload.get("modified_at", ""))
        return "\n".join(
            (
                f"[Document Review] {path or item.payload.get('name', 'document')}",
                f"Modified At: {modified_at}",
                "请审阅该文档变化，判断是否需要跟进任务或风险提示。",
            )
        )


def _as_bool(value: Any) -> bool:
    """Parse common truthy payload forms."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _csv_to_frozenset(value: Any) -> frozenset[str]:
    """Normalize comma-separated config into a lowercase frozenset."""
    if isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        values = str(value or "").split(",")
    return frozenset(
        item.strip().lower()
        for item in values
        if str(item).strip()
    )
