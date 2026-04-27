"""Compute and persist worker behavior profiles."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import frontmatter

from src.memory.episodic.linkage import compute_rule_effectiveness, load_linkage
from src.memory.episodic.models import EpisodeIndex
from src.worker.duty.models import DutyExecutionRecord
from src.worker.profile.models import (
    BehavioralTrend,
    RuleApplicationEntry,
    SkillUsageEntry,
    TaskTypeDistribution,
    WorkerBehaviorProfile,
)
from src.worker.rules.models import Rule


def compute_behavior_profile(
    worker_id: str,
    episodes: tuple[EpisodeIndex, ...],
    rules: tuple[Rule, ...],
    duty_records: tuple[DutyExecutionRecord, ...],
    current_date: str,
) -> WorkerBehaviorProfile:
    """Aggregate a worker behavior profile from existing learning artifacts."""
    current_dt = _parse_iso_date(current_date)
    last_30d_cutoff = current_dt - timedelta(days=30)

    skill_counts: Counter[str] = Counter()
    last_30d_count = 0
    task_type_counts: Counter[str] = Counter()

    for episode in episodes:
        for skill_id in episode.skills:
            if skill_id:
                skill_counts[skill_id] += 1
        if _parse_iso_date(episode.ts) >= last_30d_cutoff:
            last_30d_count += 1
        task_type_counts[_infer_task_type(episode.summary)] += 1

    total_skill_uses = sum(skill_counts.values()) or 1
    skill_usage = tuple(
        SkillUsageEntry(skill_id=skill_id, count=count, ratio=count / total_skill_uses)
        for skill_id, count in skill_counts.most_common()
    )

    effectiveness = compute_rule_effectiveness((), ())
    rule_applications = tuple(
        RuleApplicationEntry(
            rule_id=rule.rule_id,
            apply_count=rule.apply_count,
            confidence=rule.confidence,
            success_correlation=effectiveness.get(rule.rule_id, 0.0),
        )
        for rule in sorted(rules, key=lambda item: item.apply_count, reverse=True)
    )

    durations = [record.duration_seconds for record in duty_records if record.duration_seconds >= 0]
    avg_duration = sum(durations) / len(durations) if durations else 0.0
    success_rate = (
        sum(1 for record in duty_records if not record.escalated) / len(duty_records)
        if duty_records else 1.0
    )

    distribution = TaskTypeDistribution(
        writing=task_type_counts.get("writing", 0),
        coding=task_type_counts.get("coding", 0),
        analysis=task_type_counts.get("analysis", 0),
        operations=task_type_counts.get("operations", 0),
        other=task_type_counts.get("other", 0),
    )

    return WorkerBehaviorProfile(
        worker_id=worker_id,
        updated_at=current_dt.isoformat(),
        task_count_total=len(episodes),
        task_count_last_30d=last_30d_count,
        skill_usage=skill_usage,
        rule_applications=rule_applications,
        task_type_distribution=distribution,
        behavioral_trends=(),
        avg_task_duration_seconds=avg_duration,
        success_rate=success_rate,
    )


def detect_behavioral_trends(
    current: WorkerBehaviorProfile,
    previous: WorkerBehaviorProfile | None,
) -> tuple[BehavioralTrend, ...]:
    """Compare two profile snapshots and emit simple trend signals."""
    if previous is None:
        return ()

    trends: list[BehavioralTrend] = []
    current_top = current.skill_usage[0] if current.skill_usage else None
    previous_top = previous.skill_usage[0] if previous.skill_usage else None
    if (
        current_top is not None and previous_top is not None
    ):
        skill_shift_delta = current_top.ratio - previous_top.ratio
        if (
            current_top.skill_id == previous_top.skill_id
            and skill_shift_delta >= 0.2
        ) or (
            current_top.skill_id != previous_top.skill_id
            and current_top.ratio >= 0.6
        ):
            trends.append(
                BehavioralTrend(
                    trend_type="skill_shift",
                    description=(
                        f"过去 30 天 {current_top.skill_id} 占比提升至 {current_top.ratio:.0%}，"
                        f"较上次增加 {skill_shift_delta:+.0%}"
                    ),
                    confidence=min(1.0, 0.5 + abs(skill_shift_delta)),
                    detected_at=current.updated_at,
                )
            )

    if previous.avg_task_duration_seconds > 0:
        change = (
            current.avg_task_duration_seconds - previous.avg_task_duration_seconds
        ) / previous.avg_task_duration_seconds
        if abs(change) >= 0.2:
            trends.append(
                BehavioralTrend(
                    trend_type="efficiency_change",
                    description=(
                        "平均执行时长"
                        f"{'上升' if change > 0 else '下降'} {abs(change):.0%}"
                    ),
                    confidence=min(1.0, 0.5 + abs(change)),
                    detected_at=current.updated_at,
                )
            )

    prev_high = _high_confidence_ratio(previous)
    curr_high = _high_confidence_ratio(current)
    if abs(curr_high - prev_high) >= 0.2:
        trends.append(
            BehavioralTrend(
                trend_type="rule_convergence",
                description=(
                    f"高置信度规则占比变化 {(curr_high - prev_high):+.0%}"
                ),
                confidence=min(1.0, 0.5 + abs(curr_high - prev_high)),
                detected_at=current.updated_at,
            )
        )

    return tuple(trends)


def write_profile(worker_dir: Path, profile: WorkerBehaviorProfile) -> Path:
    """Persist profile to ``profile/PROFILE.md``."""
    profile_dir = worker_dir / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    path = profile_dir / "PROFILE.md"
    meta = {
        "worker_id": profile.worker_id,
        "updated_at": profile.updated_at,
        "task_count_total": profile.task_count_total,
        "task_count_last_30d": profile.task_count_last_30d,
        "avg_task_duration_seconds": profile.avg_task_duration_seconds,
        "success_rate": profile.success_rate,
        "skill_usage": [asdict(item) for item in profile.skill_usage],
        "rule_applications": [asdict(item) for item in profile.rule_applications],
        "task_type_distribution": asdict(profile.task_type_distribution),
        "behavioral_trends": [asdict(item) for item in profile.behavioral_trends],
    }
    body = _profile_body(profile)
    path.write_text(frontmatter.dumps(frontmatter.Post(body, **meta)), encoding="utf-8")
    return path


def load_profile(worker_dir: Path) -> WorkerBehaviorProfile | None:
    """Load a previously persisted profile."""
    path = worker_dir / "profile" / "PROFILE.md"
    if not path.exists():
        return None
    post = frontmatter.loads(path.read_text(encoding="utf-8"))
    meta = post.metadata
    return WorkerBehaviorProfile(
        worker_id=str(meta.get("worker_id", "")),
        updated_at=str(meta.get("updated_at", "")),
        task_count_total=int(meta.get("task_count_total", 0)),
        task_count_last_30d=int(meta.get("task_count_last_30d", 0)),
        skill_usage=tuple(
            SkillUsageEntry(**entry) for entry in meta.get("skill_usage", [])
        ),
        rule_applications=tuple(
            RuleApplicationEntry(**entry)
            for entry in meta.get("rule_applications", [])
        ),
        task_type_distribution=TaskTypeDistribution(
            **meta.get("task_type_distribution", {})
        ),
        behavioral_trends=tuple(
            BehavioralTrend(**entry) for entry in meta.get("behavioral_trends", [])
        ),
        avg_task_duration_seconds=float(meta.get("avg_task_duration_seconds", 0.0)),
        success_rate=float(meta.get("success_rate", 0.0)),
    )


def _profile_body(profile: WorkerBehaviorProfile) -> str:
    skill_lines = [f"- {entry.skill_id}: {entry.count}" for entry in profile.skill_usage]
    trend_lines = [f"- {trend.description}" for trend in profile.behavioral_trends]
    return "\n".join((
        "# Behavior Profile",
        "",
        "## Skill Usage",
        *(skill_lines or ["- none"]),
        "",
        "## Trends",
        *(trend_lines or ["- none"]),
    ))


def _infer_task_type(summary: str) -> str:
    lowered = (summary or "").lower()
    if any(token in lowered for token in ("write", "draft", "content", "copy")):
        return "writing"
    if any(token in lowered for token in ("code", "bug", "test", "deploy")):
        return "coding"
    if any(token in lowered for token in ("analy", "investig", "review", "report")):
        return "analysis"
    if any(token in lowered for token in ("alert", "monitor", "check", "duty")):
        return "operations"
    return "other"


def _parse_iso_date(value: str) -> datetime:
    normalized = (value or "").replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _high_confidence_ratio(profile: WorkerBehaviorProfile) -> float:
    if not profile.rule_applications:
        return 0.0
    high = sum(1 for entry in profile.rule_applications if entry.confidence >= 0.8)
    return high / len(profile.rule_applications)
