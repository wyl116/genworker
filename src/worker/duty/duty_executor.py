"""
DutyExecutor - converts duty triggers into TaskJob executions.

Reuses the existing WorkerRouter execution pipeline.
No new execution paths are introduced.
"""
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

from src.worker.task import TaskProvenance, create_task_manifest

from .anomaly_detector import detect_anomalies
from .execution_log import load_recent_records, write_execution_record
from .models import Duty, DutyExecutionRecord, DutyTrigger, EventContext
from .trigger_manager import select_execution_depth

logger = logging.getLogger(__name__)


def build_duty_prompt(
    duty: Duty,
    depth: str,
    trigger: DutyTrigger,
    event_context: EventContext | None = None,
) -> str:
    """
    Pure function: format a duty execution prompt.

    Includes:
    - [Duty Execution] title
    - Trigger type and description
    - Execution depth
    - Action description
    - Quality criteria

    In deep mode, appends root cause analysis requirements.
    """
    lines = [
        f"[Duty Execution] {duty.title}",
        "",
        f"Trigger: {trigger.type} ({trigger.description or trigger.id})",
        f"Execution Depth: {depth}",
    ]

    if event_context is not None:
        lines.extend([
            "",
            "## Triggering Event",
            event_context.summary(),
        ])

    lines.extend([
        "",
        "## Action",
        duty.action,
        "",
        "## Quality Criteria",
    ])

    for i, criterion in enumerate(duty.quality_criteria, 1):
        lines.append(f"{i}. {criterion}")

    if depth == "deep":
        lines.extend([
            "",
            "## Root Cause Analysis Requirements",
            "- Identify the root cause of any anomalies found",
            "- Provide a detailed analysis of contributing factors",
            "- Suggest preventive measures for future occurrences",
            "- Document the full investigation chain",
        ])

    return "\n".join(lines)


class DutyExecutor:
    """
    Converts duty trigger events into TaskJob executions.

    Uses WorkerRouter for actual execution, maintaining
    the single execution pipeline principle.
    """

    def __init__(
        self,
        worker_router,  # WorkerRouter (generic to avoid circular import)
        execution_log_dir: Path,
        duty_learning_handler: Callable[
            [DutyExecutionRecord, Duty],
            Awaitable[None] | None,
        ] | None = None,
    ) -> None:
        self._worker_router = worker_router
        self._execution_log_dir = execution_log_dir
        self._duty_learning_handler = duty_learning_handler

    async def execute(
        self,
        duty: Duty,
        trigger: DutyTrigger,
        tenant_id: str,
        worker_id: str,
        event_context: EventContext | None = None,
    ) -> DutyExecutionRecord:
        """
        Execute a duty triggered by a specific trigger.

        Flow:
        1. Determine execution depth
        2. Build prompt (action + criteria + history)
        3. Route through WorkerRouter
        4. Record execution log
        5. Check escalation conditions
        """
        depth = select_execution_depth(duty, trigger.id)
        prompt = build_duty_prompt(duty, depth, trigger, event_context)

        # Inject recent execution history into the prompt
        duty_dir = self._execution_log_dir / duty.duty_id
        recent_records = load_recent_records(duty_dir, limit=5)
        if recent_records:
            history_text = self._format_history(recent_records)
            prompt = f"{prompt}\n\n## Recent Execution History\n{history_text}"

        execution_id = uuid4().hex
        start_time = time.monotonic()
        conclusion = ""
        manifest = create_task_manifest(
            worker_id=worker_id,
            tenant_id=tenant_id,
            skill_id=duty.preferred_skill_id or "",
            preferred_skill_ids=duty.soft_preferred_skill_ids,
            provenance=TaskProvenance(
                source_type="duty_trigger",
                source_id=duty.duty_id,
                duty_id=duty.duty_id,
                trigger_id=trigger.id,
            ),
            gate_level="auto",
            task_description=prompt,
            pre_script=duty.pre_script,
        )
        task_id: str | None = manifest.task_id

        try:
            # Execute via WorkerRouter (async generator)
            result_parts: list[str] = []
            failure_message = ""
            run_failed = False
            async for event in self._worker_router.route_stream(
                task=prompt,
                tenant_id=tenant_id,
                worker_id=worker_id,
                skill_id=duty.preferred_skill_id,
                preferred_skill_ids=duty.soft_preferred_skill_ids,
                manifest=manifest,
            ):
                # Collect text content from stream events
                content = getattr(event, "content", "")
                if content:
                    result_parts.append(content)
                event_type = str(getattr(event, "event_type", "") or "")
                if event_type == "ERROR":
                    run_failed = True
                    failure_message = str(
                        getattr(event, "message", "") or "duty execution failed"
                    )
                elif event_type == "RUN_FINISHED" and not getattr(event, "success", True):
                    run_failed = True
                    failure_message = str(
                        getattr(event, "stop_reason", "") or "duty execution failed"
                    )

            if run_failed:
                conclusion = f"error: {failure_message}"
            else:
                conclusion = "".join(result_parts) if result_parts else "completed"
        except Exception as exc:
            conclusion = f"error: {exc}"
            logger.error(
                f"[DutyExecutor] Duty '{duty.duty_id}' execution failed: {exc}"
            )

        elapsed = time.monotonic() - start_time
        escalated = self._should_escalate(duty, conclusion)
        anomaly_report = detect_anomalies(
            current_conclusion=conclusion,
            current_duration=round(elapsed, 2),
            current_escalated=escalated,
            recent_records=recent_records,
        )

        record = DutyExecutionRecord(
            execution_id=execution_id,
            duty_id=duty.duty_id,
            trigger_id=trigger.id,
            depth=depth,
            executed_at=datetime.now(timezone.utc).isoformat(),
            duration_seconds=round(elapsed, 2),
            conclusion=conclusion[:500],  # Truncate for log storage
            anomalies_found=anomaly_report.anomalies,
            escalated=escalated,
            task_id=task_id,
        )

        write_execution_record(duty_dir, record)
        if self._duty_learning_handler is not None:
            try:
                await self._duty_learning_handler(record, duty)
            except Exception as exc:
                logger.warning(
                    "[DutyExecutor] duty_learning_handler failed for %s: %s",
                    duty.duty_id,
                    exc,
                )
        return record

    def _should_escalate(self, duty: Duty, conclusion: str) -> bool:
        """Check if the execution result warrants escalation."""
        if duty.escalation is None:
            return False
        # Simple keyword-based escalation check
        condition = duty.escalation.condition.lower()
        return condition in conclusion.lower()

    def _format_history(
        self,
        records: tuple[DutyExecutionRecord, ...],
    ) -> str:
        """Format recent execution records for prompt injection."""
        lines = []
        for rec in records:
            status = "escalated" if rec.escalated else "normal"
            lines.append(
                f"- [{rec.executed_at}] depth={rec.depth}, "
                f"duration={rec.duration_seconds}s, status={status}"
            )
            if rec.anomalies_found:
                for anomaly in rec.anomalies_found:
                    lines.append(f"  - anomaly: {anomaly}")
        return "\n".join(lines)
