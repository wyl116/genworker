"""
TaskRunner - async task executor wrapping engine dispatch with TaskManifest lifecycle.

Creates TaskManifest, dispatches engine, collects results, updates status.
Status flow: pending -> running -> completed | error.

Phase 7 post-processing: extracts episode candidates and rule candidates
from completed task results.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Awaitable, Callable
from uuid import uuid4

from src.common.logger import get_logger
from src.engine.checkpoint import CheckpointHandle
from src.engine.router.engine_dispatcher import EngineDispatcher
from src.engine.state import UsageBudget, WorkerContext
from src.skills.models import Skill
from src.streaming.events import (
    ErrorEvent,
    RunFinishedEvent,
    StreamEvent,
    TextMessageEvent,
    ToolCallEvent,
)
from src.tools.runtime_scope import current_execution_scope
from src.worker.scripts import inject_pre_script_output, run_pre_script

from .task import TaskManifest, TaskStore, create_task_manifest

logger = get_logger()


@dataclass(frozen=True)
class PostRunExtraction:
    """Post-run extraction results for Phase 7 hooks."""
    episode_summary: str = ""
    key_findings: tuple[str, ...] = ()
    tool_names_used: tuple[str, ...] = ()
    rule_candidates: tuple[str, ...] = ()
    applied_rule_ids: tuple[str, ...] = ()


class TaskRunner:
    """
    Async task executor that wraps engine dispatch with task manifest lifecycle.

    Responsibilities:
    1. Create a TaskManifest (pending)
    2. Mark it running, dispatch to EngineDispatcher
    3. Collect events, yield them through
    4. On completion, mark completed/error and persist
    """

    def __init__(
        self,
        engine_dispatcher: EngineDispatcher,
        task_store: TaskStore,
        post_run_handler: Callable[
            [TaskManifest, WorkerContext, PostRunExtraction],
            Awaitable[None] | None,
        ] | None = None,
        error_feedback_handler: Callable[
            [TaskManifest, WorkerContext, tuple[str, ...]],
            Awaitable[None] | None,
        ] | None = None,
        state_checkpointer: Any | None = None,
        tool_pipeline: Any | None = None,
    ) -> None:
        self._dispatcher = engine_dispatcher
        self._store = task_store
        self._post_run_handler = post_run_handler
        self._error_feedback_handler = error_feedback_handler
        self._state_checkpointer = state_checkpointer
        self._tool_pipeline = tool_pipeline

    async def execute(
        self,
        skill: Skill,
        worker_context: WorkerContext,
        task: str,
        available_tools: list[dict[str, Any]] | None = None,
        budget: UsageBudget | None = None,
        manifest: TaskManifest | None = None,
        applied_rule_ids: tuple[str, ...] = (),
        max_rounds_override: int | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """
        Execute a task with full lifecycle management.

        Creates TaskManifest, dispatches engine, updates status,
        and yields all StreamEvents.

        Args:
            skill: The matched skill.
            worker_context: Built worker context.
            task: User task description.
            available_tools: Tool definitions for the engine.
            budget: Optional token budget.

        Yields:
            StreamEvent objects from the engine.
        """
        previous_manifest = manifest
        run_id = uuid4().hex
        manifest = manifest or create_task_manifest(
            worker_id=worker_context.worker_id,
            tenant_id=worker_context.tenant_id,
            skill_id=skill.skill_id,
            task_description=task,
        )
        if not manifest.skill_id:
            from dataclasses import replace
            from src.worker.lifecycle.detectors import resolve_gate_level

            manifest = replace(
                manifest,
                skill_id=skill.skill_id,
                gate_level=resolve_gate_level(
                    skill=skill,
                    provenance=manifest.provenance,
                    task_description=manifest.task_description or task,
                ),
                task_description=manifest.task_description or task,
            )
        elif manifest.gate_level == "gated":
            from dataclasses import replace
            from src.worker.lifecycle.detectors import resolve_gate_level

            manifest = replace(
                manifest,
                gate_level=resolve_gate_level(
                    skill=skill,
                    provenance=manifest.provenance,
                    task_description=manifest.task_description or task,
                ),
            )

        # pending -> running
        manifest = manifest.mark_running(run_id=run_id)
        self._store.save(manifest)
        checkpoint_handle = CheckpointHandle(
            tenant_id=manifest.tenant_id,
            worker_id=manifest.worker_id,
            task_id=manifest.task_id,
            run_id=manifest.run_id,
            thread_id=manifest.main_session_key or manifest.task_id,
        )
        resume_from = None
        if (
            self._state_checkpointer is not None
            and previous_manifest is not None
            and previous_manifest.status.value == "running"
            and previous_manifest.run_id
        ):
            resume_from = await self._state_checkpointer.load_latest(
                tenant_id=previous_manifest.tenant_id,
                worker_id=previous_manifest.worker_id,
                task_id=previous_manifest.task_id,
                run_id=previous_manifest.run_id,
            )

        collected_text: list[str] = []
        collected_tools: list[str] = []
        error_occurred = False
        error_msg = ""

        try:
            if manifest.pre_script is not None:
                scope = current_execution_scope()
                if scope is None or self._tool_pipeline is None:
                    task = inject_pre_script_output(
                        task,
                        "[pre_script error: missing execution scope or tool pipeline]",
                    )
                else:
                    script_output = await run_pre_script(
                        pre_script=manifest.pre_script,
                        scope=scope,
                        pipeline=self._tool_pipeline,
                    )
                    task = inject_pre_script_output(task, script_output)

            async for event in self._dispatcher.dispatch(
                skill=skill,
                worker_context=worker_context,
                task=task,
                available_tools=available_tools,
                budget=budget,
                run_id=run_id,
                max_rounds_override=max_rounds_override,
                checkpoint_handle=checkpoint_handle,
                resume_from=resume_from,
            ):
                # Collect text for result summary
                if isinstance(event, TextMessageEvent):
                    collected_text.append(event.content)
                elif isinstance(event, ToolCallEvent):
                    collected_tools.append(event.tool_name)
                elif isinstance(event, ErrorEvent):
                    error_occurred = True
                    error_msg = event.message
                elif isinstance(event, RunFinishedEvent) and not event.success:
                    error_occurred = True
                    error_msg = event.stop_reason or "Run finished unsuccessfully"

                yield event

        except Exception as exc:
            error_occurred = True
            error_msg = str(exc)
            logger.error(
                f"[TaskRunner] Task {manifest.task_id} failed: {exc}",
                exc_info=True,
            )
            yield ErrorEvent(
                run_id=run_id,
                code="TASK_RUNNER_ERROR",
                message=error_msg,
            )

        # running -> completed | error
        if error_occurred:
            manifest = manifest.mark_error(error_msg)
        else:
            summary = "\n".join(collected_text)[:500] if collected_text else ""
            manifest = manifest.mark_completed(result_summary=summary)

        self._store.save(manifest)

        # Phase 7 post-processing: extract episode and rule candidates
        if not error_occurred:
            extraction = extract_post_run_data(
                task=task,
                collected_text=collected_text,
                tool_names_used=tuple(dict.fromkeys(collected_tools)),
            )
            extraction = dataclasses.replace(
                extraction,
                applied_rule_ids=applied_rule_ids,
            )
            self._last_extraction = extraction
            if self._post_run_handler is not None:
                try:
                    await self._post_run_handler(
                        manifest, worker_context, extraction,
                    )
                except Exception as exc:
                    logger.warning(
                        "[TaskRunner] Post-run handler failed for %s: %s",
                        manifest.task_id,
                        exc,
                    )
        else:
            self._last_extraction = None
            if self._error_feedback_handler is not None:
                try:
                    await self._error_feedback_handler(
                        manifest,
                        worker_context,
                        applied_rule_ids,
                    )
                except Exception as exc:
                    logger.warning(
                        "[TaskRunner] Error feedback handler failed for %s: %s",
                        manifest.task_id,
                        exc,
                    )

        logger.info(
            f"[TaskRunner] Task {manifest.task_id} "
            f"finished with status={manifest.status.value}"
        )

    @property
    def last_extraction(self) -> PostRunExtraction | None:
        """Return the last post-run extraction, if any."""
        return getattr(self, "_last_extraction", None)


def extract_post_run_data(
    task: str,
    collected_text: list[str],
    tool_names_used: tuple[str, ...],
) -> PostRunExtraction:
    """
    Extract episode summary, key findings, and rule candidates
    from completed task results.

    Pure function operating on collected event data.
    """
    full_text = "\n".join(collected_text)
    summary = full_text[:200] if full_text else task[:200]

    # Extract key findings: sentences that look like conclusions
    findings = _extract_findings(full_text)

    # Rule candidates: patterns suggesting behavioral lessons
    rule_candidates = _extract_rule_candidates(full_text)

    return PostRunExtraction(
        episode_summary=summary,
        key_findings=findings,
        tool_names_used=tool_names_used,
        rule_candidates=rule_candidates,
    )


def _extract_findings(text: str) -> tuple[str, ...]:
    """Extract key findings from result text."""
    if not text:
        return ()
    # Simple heuristic: lines starting with key indicators
    indicators = ("found", "result", "conclusion", "summary", "note")
    findings: list[str] = []
    for line in text.splitlines():
        stripped = line.strip().lower()
        if any(stripped.startswith(ind) for ind in indicators):
            findings.append(line.strip())
        if len(findings) >= 5:
            break
    return tuple(findings)


def _extract_rule_candidates(text: str) -> tuple[str, ...]:
    """Extract potential rule candidates from result text."""
    if not text:
        return ()
    # Simple heuristic: lines with lesson-like patterns
    patterns = ("should", "always", "never", "best practice", "recommend")
    candidates: list[str] = []
    for line in text.splitlines():
        stripped = line.strip().lower()
        if any(p in stripped for p in patterns):
            candidates.append(line.strip())
        if len(candidates) >= 3:
            break
    return tuple(candidates)
