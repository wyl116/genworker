"""Pure anomaly detection for duty execution records."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .models import DutyExecutionRecord


@dataclass(frozen=True)
class AnomalyReport:
    """Duty execution anomaly report."""

    anomalies: tuple[str, ...]
    baseline_duration_mean: float
    baseline_duration_stddev: float
    current_duration: float


def detect_anomalies(
    current_conclusion: str,
    current_duration: float,
    current_escalated: bool,
    recent_records: tuple[DutyExecutionRecord, ...],
) -> AnomalyReport:
    """Detect duration, conclusion, and escalation anomalies."""
    if not recent_records:
        return AnomalyReport((), 0.0, 0.0, current_duration)

    anomalies: list[str] = []
    durations = tuple(record.duration_seconds for record in recent_records)
    mean = sum(durations) / len(durations)
    stddev = _compute_stddev(durations, mean) if len(durations) >= 3 else 0.0

    if len(durations) >= 3 and current_duration > mean + (2 * stddev):
        anomalies.append(
            f"duration_anomaly: current={current_duration:.2f}s baseline={mean:.2f}s±{stddev:.2f}s"
        )

    history_error_rate = _keyword_rate(recent_records, ("error", "failed", "exception"))
    if _contains_keywords(current_conclusion, ("error", "failed", "exception")) and history_error_rate < 0.1:
        anomalies.append("conclusion_anomaly: unexpected error pattern")

    escalation_rate = (
        sum(1 for record in recent_records if record.escalated) / len(recent_records)
        if recent_records else 0.0
    )
    if current_escalated and escalation_rate < 0.05:
        anomalies.append("escalation_anomaly: rare escalation pattern")

    return AnomalyReport(tuple(anomalies), mean, stddev, current_duration)


def _compute_stddev(durations: tuple[float, ...], mean: float) -> float:
    variance = sum((duration - mean) ** 2 for duration in durations) / len(durations)
    return math.sqrt(variance)


def _keyword_rate(records: tuple[DutyExecutionRecord, ...], keywords: tuple[str, ...]) -> float:
    hits = sum(1 for record in records if _contains_keywords(record.conclusion, keywords))
    return hits / len(records) if records else 0.0


def _contains_keywords(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = (text or "").lower()
    return any(keyword in lowered for keyword in keywords)
