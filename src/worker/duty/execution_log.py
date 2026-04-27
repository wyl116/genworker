"""
Execution log - append-only JSONL storage for duty execution records.

Provides:
- write_execution_record: append a record to execution_log.jsonl
- load_recent_records: read the most recent N records
"""
import json
from dataclasses import asdict
from pathlib import Path

from .models import DutyExecutionRecord


def write_execution_record(duty_dir: Path, record: DutyExecutionRecord) -> None:
    """
    Append an execution record to duty_dir/execution_log.jsonl.

    Creates the directory and file if they don't exist.
    """
    duty_dir.mkdir(parents=True, exist_ok=True)
    log_path = duty_dir / "execution_log.jsonl"

    data = asdict(record)
    # Convert tuple fields to lists for JSON serialization
    data["anomalies_found"] = list(record.anomalies_found)

    line = json.dumps(data, ensure_ascii=False)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_recent_records(
    duty_dir: Path,
    limit: int = 10,
) -> tuple[DutyExecutionRecord, ...]:
    """
    Read the most recent N execution records from execution_log.jsonl.

    Returns records in chronological order (oldest first).
    Returns empty tuple if file doesn't exist.
    """
    log_path = duty_dir / "execution_log.jsonl"
    if not log_path.is_file():
        return ()

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    recent_lines = lines[-limit:] if len(lines) > limit else lines

    records: list[DutyExecutionRecord] = []
    for line in recent_lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            records.append(_record_from_dict(data))
        except (json.JSONDecodeError, KeyError):
            continue

    return tuple(records)


def _record_from_dict(data: dict) -> DutyExecutionRecord:
    """Deserialize a DutyExecutionRecord from a dict."""
    anomalies = data.get("anomalies_found", [])
    return DutyExecutionRecord(
        execution_id=data["execution_id"],
        duty_id=data["duty_id"],
        trigger_id=data["trigger_id"],
        depth=data["depth"],
        executed_at=data["executed_at"],
        duration_seconds=float(data["duration_seconds"]),
        conclusion=data.get("conclusion", ""),
        anomalies_found=tuple(anomalies),
        escalated=data.get("escalated", False),
        task_id=data.get("task_id"),
    )
