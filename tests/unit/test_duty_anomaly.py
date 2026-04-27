# edition: baseline
from src.worker.duty.anomaly_detector import detect_anomalies
from src.worker.duty.models import DutyExecutionRecord


def _record(duration: float, conclusion: str = "ok", escalated: bool = False) -> DutyExecutionRecord:
    return DutyExecutionRecord(
        execution_id="e1",
        duty_id="d1",
        trigger_id="t1",
        depth="standard",
        executed_at="2026-04-09T00:00:00+00:00",
        duration_seconds=duration,
        conclusion=conclusion,
        escalated=escalated,
    )


def test_detect_anomalies_empty_history():
    result = detect_anomalies("ok", 10.0, False, ())
    assert result.anomalies == ()
    assert result.baseline_duration_mean == 0.0


def test_detect_duration_anomaly_with_baseline():
    history = (_record(2.0), _record(2.0), _record(2.0))
    result = detect_anomalies("ok", 6.5, False, history)
    assert any("duration_anomaly" in item for item in result.anomalies)


def test_skip_duration_when_sample_too_small():
    history = (_record(2.0), _record(2.2))
    result = detect_anomalies("error happened", 10.0, False, history)
    assert not any("duration_anomaly" in item for item in result.anomalies)
    assert any("conclusion_anomaly" in item for item in result.anomalies)


def test_detect_escalation_anomaly():
    history = (_record(2.0), _record(2.1), _record(2.2))
    result = detect_anomalies("ok", 2.0, True, history)
    assert any("escalation_anomaly" in item for item in result.anomalies)
