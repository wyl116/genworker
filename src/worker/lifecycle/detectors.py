"""Lifecycle detectors for task, goal, and duty transitions."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from src.common.logger import get_logger
from src.worker.duty.execution_log import load_recent_records
from src.worker.duty.parser import parse_duty
from src.worker.goal.planner import goal_to_duty
from src.worker.task import TaskManifest, TaskStore, TaskStatus

from .duty_builder import stable_duty_id
from .models import SuggestionRecord, add_days_iso, now_iso, parse_iso
from .suggestion_store import SuggestionStore
from .feedback_store import FeedbackStore

logger = get_logger()

_DATE_PATTERN = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}")
_RELATIVE_PERIOD_PATTERN = re.compile(r"(上|本|下)(周|月|季)")
_NUMBER_PATTERN = re.compile(r"\d{3,}")
_URL_PATTERN = re.compile(r"https?://\S+")
_PATH_PATTERN = re.compile(r"/[\w./-]+")
_WHITESPACE_PATTERN = re.compile(r"\s+")
_RISKY_ENGLISH_PATTERN = re.compile(r"\b(delete|send|modify|sync|write|update|remove)\b")


def normalize_task_description(text: str) -> str:
    """Normalize task descriptions for repeated manual task clustering."""
    normalized = str(text or "").strip()
    normalized = _DATE_PATTERN.sub("{DATE}", normalized)
    normalized = _RELATIVE_PERIOD_PATTERN.sub("{PERIOD}", normalized)
    normalized = _NUMBER_PATTERN.sub("{NUM}", normalized)
    normalized = _URL_PATTERN.sub("{URL}", normalized)
    normalized = _PATH_PATTERN.sub("{PATH}", normalized)
    normalized = normalized.replace("，", ",").replace("。", ".").replace("：", ":")
    normalized = _WHITESPACE_PATTERN.sub(" ", normalized)
    return normalized.strip().lower()


def resolve_gate_level(*, skill=None, provenance=None, task_description: str = "") -> str:
    """Resolve derived task gate level."""
    level = str(getattr(skill, "gate_level", "gated") or "gated")
    source_type = str(getattr(provenance, "source_type", "") or "")
    if getattr(provenance, "suggestion_id", ""):
        return "gated"
    task_text = str(task_description or "")
    risky_keywords = ("删除", "发送", "修改", "同步", "写入", "批量")
    lowered = task_text.lower()
    if any(keyword in task_text for keyword in risky_keywords) or _RISKY_ENGLISH_PATTERN.search(lowered):
        return "gated"
    if source_type in {"goal_followup", "goal_task", "duty_trigger", "heartbeat"}:
        return "auto"
    return "auto" if level == "auto" else "gated"


@dataclass(frozen=True)
class RepeatedTaskDetector:
    """Detect repeated manual tasks that should become duties."""

    task_store: TaskStore
    suggestion_store: SuggestionStore

    def detect(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        lookback_days: int = 14,
        min_count: int = 5,
    ) -> tuple[SuggestionRecord, ...]:
        now_dt = parse_iso(now_iso())
        if now_dt is None:
            return ()
        clusters: dict[str, list[TaskManifest]] = defaultdict(list)
        for manifest in self.task_store.list_by_worker(tenant_id, worker_id):
            if manifest.status != TaskStatus.COMPLETED:
                continue
            if manifest.provenance.source_type != "manual":
                continue
            if manifest.provenance.goal_id or manifest.provenance.duty_id:
                continue
            created_at = parse_iso(manifest.created_at)
            if created_at is None or (now_dt - created_at).days >= lookback_days:
                continue
            key = normalize_task_description(manifest.task_description)
            if key:
                clusters[key].append(manifest)

        created: list[SuggestionRecord] = []
        self.suggestion_store.expire_pending(tenant_id, worker_id)
        for cluster_key, manifests in clusters.items():
            if len(manifests) < min_count:
                continue
            suggestion = SuggestionRecord(
                suggestion_id=f"sugg-{uuid4().hex[:8]}",
                type="task_to_duty",
                source_entity_type="task_cluster",
                source_entity_id=cluster_key,
                title=f"建议将重复任务转为 Duty: {manifests[0].task_description[:40]}",
                reason=f"检测到近 {lookback_days} 天内重复执行 {len(manifests)} 次的手动任务。",
                evidence=tuple(manifest.task_id for manifest in manifests[-5:]),
                confidence=min(0.99, 0.5 + len(manifests) * 0.08),
                candidate_payload=json.dumps(
                    {
                        "tenant_id": tenant_id,
                        "worker_id": worker_id,
                        "duty_id": _build_task_cluster_duty_id(cluster_key),
                        "title": _build_task_cluster_title(manifests[0].task_description),
                        "schedule": _infer_schedule(manifests),
                        "action": manifests[0].task_description,
                        "quality_criteria": ["按既有手工任务标准完成", "输出可复核结果"],
                        "preferred_skill_ids": list(manifests[0].preferred_skill_ids),
                        "source_task_ids": [manifest.task_id for manifest in manifests],
                    },
                    ensure_ascii=False,
                ),
                expires_at=add_days_iso(now_iso(), 30),
            )
            created_record = self.suggestion_store.create(tenant_id, worker_id, suggestion)
            if created_record is not None:
                created.append(created_record)
        return tuple(created)


@dataclass(frozen=True)
class GoalCompletionAdvisor:
    """Create goal_to_duty suggestions for eligible completed goals."""

    suggestion_store: SuggestionStore
    llm_client: object | None

    async def detect(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        goals_dir: Path,
    ) -> tuple[SuggestionRecord, ...]:
        from src.runtime.scheduler_runtime import load_unique_goals

        self.suggestion_store.expire_pending(tenant_id, worker_id)
        created: list[SuggestionRecord] = []
        for _, goal in load_unique_goals(goals_dir):
            if goal.status != "completed" or goal.on_complete != "create_duty":
                continue
            if self.llm_client is None:
                continue
            if self.suggestion_store.creation_block_reason(
                tenant_id=tenant_id,
                worker_id=worker_id,
                suggestion_type="goal_to_duty",
                source_entity_id=goal.goal_id,
            ) is not None:
                continue
            duty = await goal_to_duty(goal, self.llm_client)
            if duty is None:
                continue
            suggestion = SuggestionRecord(
                suggestion_id=f"sugg-{uuid4().hex[:8]}",
                type="goal_to_duty",
                source_entity_type="goal",
                source_entity_id=goal.goal_id,
                title=f"建议将 Goal 转为 Duty: {goal.title}",
                reason=f"Goal '{goal.title}' 已完成，且 on_complete=create_duty。",
                evidence=(goal.goal_id,),
                confidence=0.9,
                candidate_payload=json.dumps(
                    {
                        "tenant_id": tenant_id,
                        "worker_id": worker_id,
                        "duty_id": duty.duty_id,
                        "title": duty.title,
                        "action": duty.action,
                        "quality_criteria": list(duty.quality_criteria),
                        "preferred_skill_ids": list(duty.preferred_skill_ids),
                        "source_goal_id": goal.goal_id,
                    },
                    ensure_ascii=False,
                ),
                expires_at=add_days_iso(now_iso(), 30),
            )
            created_record = self.suggestion_store.create(tenant_id, worker_id, suggestion)
            if created_record is not None:
                created.append(created_record)
        return tuple(created)


@dataclass(frozen=True)
class DutyDriftDetector:
    """Detect duties whose outputs are repeatedly rejected or degraded."""

    suggestion_store: SuggestionStore
    feedback_store: FeedbackStore

    def detect(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        duties_dir: Path,
    ) -> tuple[SuggestionRecord, ...]:
        self.suggestion_store.expire_pending(tenant_id, worker_id)
        if not duties_dir.is_dir():
            return ()
        created: list[SuggestionRecord] = []
        for duty_id in _iter_canonical_duty_ids(duties_dir):
            records = load_recent_records(duties_dir / duty_id, limit=10)
            feedback = self.feedback_store.list_for_target(
                tenant_id,
                worker_id,
                target_type="duty",
                target_id=duty_id,
            )
            if not _should_flag_duty(records, feedback):
                continue
            suggested_changes = _build_duty_drift_changes(records, feedback)
            suggestion = SuggestionRecord(
                suggestion_id=f"sugg-{uuid4().hex[:8]}",
                type="duty_redefine",
                source_entity_type="duty",
                source_entity_id=duty_id,
                title=f"建议重新定义 Duty: {duty_id}",
                reason="Duty 多次被否定或异常指标显著上升。",
                evidence=tuple(record.execution_id for record in records[-3:]) or tuple(
                    item.feedback_id for item in feedback[-3:]
                ),
                confidence=0.85,
                candidate_payload=json.dumps(
                    {
                        "tenant_id": tenant_id,
                        "worker_id": worker_id,
                        "duty_id": duty_id,
                        "recommended_action": suggested_changes["recommended_action"],
                        "suggested_changes": suggested_changes["suggested_changes"],
                    },
                    ensure_ascii=False,
                ),
                expires_at=add_days_iso(now_iso(), 30),
            )
            created_record = self.suggestion_store.create(tenant_id, worker_id, suggestion)
            if created_record is not None:
                created.append(created_record)
        return tuple(created)


def _build_task_cluster_title(task_description: str) -> str:
    text = str(task_description or "").strip()
    if text.startswith("检查"):
        return f"定期{text}"
    return f"定期执行: {text[:32]}"


def _build_task_cluster_duty_id(cluster_key: str) -> str:
    return stable_duty_id(str(cluster_key or ""), prefix="duty")


def _infer_schedule(manifests: Iterable[TaskManifest]) -> str:
    items = tuple(manifests)
    timestamps = [parse_iso(item.created_at) for item in items]
    timestamps = [item for item in timestamps if item is not None]
    if not timestamps:
        return "0 9 * * 1"
    weekday_counter = Counter(item.weekday() for item in timestamps)
    hour_counter = Counter(item.hour for item in timestamps)
    top_weekday, weekday_hits = weekday_counter.most_common(1)[0]
    top_hour, hour_hits = hour_counter.most_common(1)[0]
    total = len(timestamps)
    if weekday_hits / total >= 0.6 and hour_hits / total >= 0.6:
        cron_weekday = (top_weekday + 1) % 7
        return f"0 {top_hour} * * {cron_weekday}"
    if hour_hits / total >= 0.6:
        return f"0 {top_hour} * * *"
    if weekday_hits / total >= 0.6:
        cron_weekday = (top_weekday + 1) % 7
        return f"0 9 * * {cron_weekday}"
    return "0 9 * * 1"


def _should_flag_duty(records, feedback) -> bool:
    recent_feedback = feedback[-10:]
    rejection_flags = [item for item in recent_feedback if item.verdict == "rejected"]
    if len(rejection_flags) >= 3 and all(item.verdict == "rejected" for item in recent_feedback[-3:]):
        return True
    if recent_feedback and len(rejection_flags) / len(recent_feedback) > 0.4:
        return True
    if records:
        recent = records[-10:]
        if recent and sum(1 for record in recent if record.escalated) / len(recent) >= 0.5:
            return True
        if recent and sum(len(record.anomalies_found) for record in recent) / len(recent) >= 2:
            return True
    return False


def _build_duty_drift_changes(records, feedback) -> dict:
    recent_feedback = feedback[-3:]
    reasons = [item.reason for item in recent_feedback if item.reason]
    if recent_feedback and all(item.verdict == "rejected" for item in recent_feedback):
        return {
            "recommended_action": "redefine_action",
            "suggested_changes": {
                "action": "重新核对数据源和产出格式后再执行原职责",
                "quality_criteria": reasons or ["需要与当前数据源格式一致", "需要通过人工抽样复核"],
            },
        }
    if records and sum(1 for record in records[-10:] if record.escalated) >= 5:
        return {
            "recommended_action": "pause",
            "suggested_changes": {},
        }
    return {
        "recommended_action": "tighten_quality_criteria",
        "suggested_changes": {
            "quality_criteria": reasons or ["补充更严格的校验标准"],
        },
    }


def _iter_canonical_duty_ids(duties_dir: Path) -> tuple[str, ...]:
    """Yield canonical duty IDs from parsed DUTY definitions."""
    duty_ids: list[str] = []
    seen: set[str] = set()
    for duty_file in sorted(duties_dir.glob("*.md")):
        try:
            duty = parse_duty(duty_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[DutyDriftDetector] Failed to parse %s: %s", duty_file, exc)
            continue
        if duty.duty_id in seen:
            logger.warning(
                "[DutyDriftDetector] Duplicate duty_id=%s found in %s; skipping duplicate",
                duty.duty_id,
                duty_file,
            )
            continue
        seen.add(duty.duty_id)
        duty_ids.append(duty.duty_id)
    return tuple(duty_ids)
