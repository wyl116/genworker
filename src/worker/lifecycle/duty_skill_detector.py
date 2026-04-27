"""Lifecycle detector: stable duties that should evolve into skills."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from src.common.logger import get_logger
from src.worker.duty.execution_log import load_recent_records
from src.worker.duty.parser import parse_duty

from .models import SuggestionRecord, add_days_iso, now_iso
from .skill_builder import extract_keywords_from_text, stable_skill_id
from .suggestion_store import SuggestionStore

logger = get_logger()

_FAILURE_KEYWORDS = frozenset(("error", "failed", "exception", "timeout", "crash"))


@dataclass(frozen=True)
class DutySkillDetector:
    """Detect duties stable enough to be abstracted into reusable skills."""

    suggestion_store: SuggestionStore

    def detect(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        duties_dir: Path,
        min_executions: int = 10,
        max_anomaly_rate: float = 0.2,
        max_escalation_rate: float = 0.1,
        min_success_rate: float = 0.8,
        lookback_records: int = 20,
    ) -> tuple[SuggestionRecord, ...]:
        """Scan active duties and create ``duty_to_skill`` suggestions."""
        self.suggestion_store.expire_pending(tenant_id, worker_id)
        if not duties_dir.is_dir():
            return ()

        created: list[SuggestionRecord] = []
        seen: set[str] = set()
        for duty_file in sorted(duties_dir.glob("*.md")):
            try:
                duty = parse_duty(duty_file.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("[DutySkillDetector] Failed to parse %s: %s", duty_file, exc)
                continue

            if duty.duty_id in seen:
                continue
            seen.add(duty.duty_id)
            if duty.status != "active" or duty.skill_id:
                continue

            records = load_recent_records(duties_dir / duty.duty_id, limit=lookback_records)
            if len(records) < min_executions:
                continue

            anomaly_count = sum(1 for record in records if record.anomalies_found)
            escalation_count = sum(1 for record in records if record.escalated)
            failure_count = sum(1 for record in records if _is_failure_conclusion(record.conclusion))
            total = len(records)

            if anomaly_count / total > max_anomaly_rate:
                continue
            if escalation_count / total > max_escalation_rate:
                continue
            if (total - failure_count) / total < min_success_rate:
                continue

            payload = _build_skill_payload(duty)
            suggestion = SuggestionRecord(
                suggestion_id=f"sugg-{uuid4().hex[:8]}",
                type="duty_to_skill",
                source_entity_type="duty",
                source_entity_id=duty.duty_id,
                title=f"建议将 Duty 演化为 Skill: {duty.title[:40]}",
                reason=(
                    f"Duty '{duty.title}' 已稳定执行 {total} 次，"
                    f"异常率 {anomaly_count / total:.0%}，升级率 {escalation_count / total:.0%}。"
                ),
                evidence=tuple(record.execution_id for record in records[-5:]),
                confidence=min(0.95, 0.5 + total * 0.03),
                candidate_payload=json.dumps(payload, ensure_ascii=False),
                expires_at=add_days_iso(now_iso(), 30),
            )
            created_record = self.suggestion_store.create(tenant_id, worker_id, suggestion)
            if created_record is not None:
                created.append(created_record)
        return tuple(created)


def run_duty_skill_detection(
    tenant_id: str,
    worker_id: str,
    workspace_root: Path,
    suggestion_store: SuggestionStore,
) -> tuple[SuggestionRecord, ...]:
    """Cron entrypoint for ``duty_to_skill`` suggestion generation."""
    detector = DutySkillDetector(suggestion_store=suggestion_store)
    worker_dir = workspace_root / "tenants" / tenant_id / "workers" / worker_id
    return detector.detect(
        tenant_id=tenant_id,
        worker_id=worker_id,
        duties_dir=worker_dir / "duties",
    )


def _build_skill_payload(duty) -> dict:
    """Convert one stable duty into a skill suggestion payload."""
    skill_id = stable_skill_id(
        f"{duty.duty_id}:{duty.action}:{'|'.join(duty.quality_criteria)}"
    )
    keywords = extract_keywords_from_text(f"{duty.title} {duty.action}")
    return {
        "skill_id": skill_id,
        "name": skill_id,
        "description": f"自动从 Duty '{duty.title}' 演化的技能",
        "keywords": list(keywords),
        "strategy_mode": "autonomous",
        "instructions_seed": duty.action,
        "instructions_reason": "",
        "quality_criteria": list(duty.quality_criteria),
        "recommended_tools": [],
        "source_type": "duty",
        "source_duty_id": duty.duty_id,
    }


def _is_failure_conclusion(conclusion: str) -> bool:
    """Catch persistent failures that anomaly detection may not classify."""
    lowered = str(conclusion or "").strip().lower()
    return any(keyword in lowered for keyword in _FAILURE_KEYWORDS)
