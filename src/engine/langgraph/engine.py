"""LangGraph execution engine."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncGenerator

from langgraph.types import Command

from src.autonomy.inbox import SessionInboxStore
from src.common.logger import get_logger
from src.engine.protocols import LLMClient, ToolExecutor
from src.engine.state import UsageBudget, WorkerContext
from src.services.llm.intent import LLMCallIntent, Purpose
from src.skills.loader import SkillLoader
from src.skills.models import NodeDefinition, Skill
from src.streaming.adapters.langgraph_adapter import LangGraphStreamAdapter
from src.streaming.events import (
    ApprovalPendingEvent,
    BudgetExceededEvent,
    ErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StreamEvent,
    TextMessageEvent,
)

from .builder import build_graph_bundle
from .checkpointer import LangGraphCheckpointer
from .context import NodeContext
from .digest import compute_state_digest
from .interrupt_bridge import InterruptBridge
from .models import BudgetExceededError, BudgetTracker, CompiledGraphBundle, LangGraphInitError, StateDriftError

logger = get_logger()


class LangGraphEngine:
    """Execute declarative or Python langgraph skills."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        checkpointer: LangGraphCheckpointer,
        tool_executor: ToolExecutor,
        llm_client: LLMClient,
        inbox_store: SessionInboxStore,
        stream_adapter: LangGraphStreamAdapter | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root)
        self._checkpointer = checkpointer
        self._tools = tool_executor
        self._llm = llm_client
        self._stream_adapter = stream_adapter or LangGraphStreamAdapter()
        self._interrupt_bridge = InterruptBridge(inbox_store=inbox_store)
        self._skill_loader = SkillLoader()

    async def execute(
        self,
        skill: Skill,
        worker_context: WorkerContext,
        task: str,
        *,
        available_tools: list[dict[str, Any]] | None = None,
        budget: UsageBudget | None = None,
        run_id: str,
        checkpoint_handle: Any | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        del checkpoint_handle
        budget_tracker = BudgetTracker.from_budget(budget)
        bundle = build_graph_bundle(
            skill=skill,
            node_context=self._make_context(
                skill=skill,
                worker_context=worker_context,
                thread_id=run_id,
                budget=budget_tracker.snapshot(),
                available_tools=available_tools,
            ),
            budget_tracker=budget_tracker,
        )
        config = self._config_for(
            thread_id=run_id,
            tenant_id=worker_context.tenant_id,
            worker_id=worker_context.worker_id,
            skill=skill,
            whitelist=bundle.state_whitelist,
        )
        yield RunStartedEvent(run_id=run_id, thread_id=run_id)
        try:
            async for event in self._run_stream(
                bundle=bundle,
                stream=bundle.compiled.astream_events(
                    self._initial_state(task, bundle.state_whitelist),
                    config=config,
                    version="v2",
                    recursion_limit=bundle.max_steps,
                ),
                run_id=run_id,
            ):
                yield event
            paused = await self._emit_pause_if_needed(
                bundle=bundle,
                config=config,
                run_id=run_id,
                tenant_id=worker_context.tenant_id,
                worker_id=worker_context.worker_id,
                skill=skill,
            )
            if paused is not None:
                yield paused
                yield RunFinishedEvent(
                    run_id=run_id,
                    success=True,
                    stop_reason="approval_pending",
                )
                return
            async for event in self._emit_final_state_texts(
                bundle=bundle,
                config=config,
                run_id=run_id,
            ):
                yield event
        except BudgetExceededError:
            yield BudgetExceededEvent(
                run_id=run_id,
                max_tokens=budget_tracker.max_tokens,
                used_tokens=budget_tracker.used_tokens,
            )
            yield RunFinishedEvent(
                run_id=run_id,
                success=True,
                stop_reason="budget_exceeded",
            )
            return
        except Exception as exc:
            logger.error("[LangGraphEngine] execute failed: %s", exc, exc_info=True)
            yield ErrorEvent(
                run_id=run_id,
                code="LANGGRAPH_EXECUTION_ERROR",
                message=str(exc),
            )
            yield RunFinishedEvent(run_id=run_id, success=False, stop_reason=str(exc))
            return
        yield RunFinishedEvent(run_id=run_id, success=True)

    async def resume(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        thread_id: str,
        skill_id: str,
        decision: dict[str, Any],
        expected_digest: str,
        inbox_id: str = "",
    ) -> AsyncGenerator[StreamEvent, None]:
        record = await self._checkpointer.load_by_thread(thread_id)
        if record is None:
            yield ErrorEvent(
                run_id=thread_id,
                code="LANGGRAPH_THREAD_NOT_FOUND",
                message=f"Thread '{thread_id}' not found",
            )
            yield RunFinishedEvent(run_id=thread_id, success=False, stop_reason="thread_not_found")
            return
        skill = self._load_skill(
            tenant_id=tenant_id,
            worker_id=worker_id,
            skill_id=skill_id,
            source_path=record.source_path,
        )
        if skill is None:
            yield ErrorEvent(
                run_id=thread_id,
                code="LANGGRAPH_SKILL_NOT_FOUND",
                message=f"Skill '{skill_id}' not found for resume",
            )
            yield RunFinishedEvent(run_id=thread_id, success=False, stop_reason="skill_not_found")
            return
        budget_tracker = BudgetTracker()
        bundle = build_graph_bundle(
            skill=skill,
            node_context=self._make_context(
                skill=skill,
                worker_context=WorkerContext(
                    worker_id=worker_id,
                    tenant_id=tenant_id,
                    skill_id=skill_id,
                ),
                thread_id=thread_id,
                budget=budget_tracker.snapshot(),
                available_tools=None,
            ),
            budget_tracker=budget_tracker,
        )
        config = self._config_for(
            thread_id=thread_id,
            tenant_id=tenant_id,
            worker_id=worker_id,
            skill=skill,
            whitelist=bundle.state_whitelist,
        )
        snapshot = await bundle.compiled.aget_state(config)
        current_digest = compute_state_digest(
            dict(getattr(snapshot, "values", {}) or {}),
            bundle.state_whitelist,
        )
        if expected_digest and current_digest != expected_digest:
            message = (
                f"State digest mismatch for thread '{thread_id}': "
                f"expected={expected_digest} current={current_digest}"
            )
            yield ErrorEvent(
                run_id=thread_id,
                code="LANGGRAPH_STATE_DRIFT",
                message=message,
            )
            yield RunFinishedEvent(run_id=thread_id, success=False, stop_reason="state_drift")
            return
        yield RunStartedEvent(run_id=thread_id, thread_id=thread_id)
        try:
            async for event in self._run_stream(
                bundle=bundle,
                stream=bundle.compiled.astream_events(
                    Command(
                        update={
                            "_approval_decision": dict(decision),
                            "_approval_inbox_id": inbox_id,
                        }
                    ),
                    config=config,
                    version="v2",
                    recursion_limit=bundle.max_steps,
                ),
                run_id=thread_id,
            ):
                yield event
            paused = await self._emit_pause_if_needed(
                bundle=bundle,
                config=config,
                run_id=thread_id,
                tenant_id=tenant_id,
                worker_id=worker_id,
                skill=skill,
            )
            if paused is not None:
                yield paused
                yield RunFinishedEvent(
                    run_id=thread_id,
                    success=True,
                    stop_reason="approval_pending",
                )
                return
            async for event in self._emit_final_state_texts(
                bundle=bundle,
                config=config,
                run_id=thread_id,
            ):
                yield event
        except BudgetExceededError:
            yield BudgetExceededEvent(
                run_id=thread_id,
                max_tokens=budget_tracker.max_tokens,
                used_tokens=budget_tracker.used_tokens,
            )
            yield RunFinishedEvent(
                run_id=thread_id,
                success=True,
                stop_reason="budget_exceeded",
            )
            return
        except Exception as exc:
            logger.error("[LangGraphEngine] resume failed: %s", exc, exc_info=True)
            yield ErrorEvent(
                run_id=thread_id,
                code="LANGGRAPH_RESUME_ERROR",
                message=str(exc),
            )
            yield RunFinishedEvent(run_id=thread_id, success=False, stop_reason=str(exc))
            return
        yield RunFinishedEvent(run_id=thread_id, success=True)

    def _make_context(
        self,
        *,
        skill: Skill,
        worker_context: WorkerContext,
        thread_id: str,
        budget: UsageBudget,
        available_tools: list[dict[str, Any]] | None,
    ) -> NodeContext:
        return NodeContext(
            worker_context=replace(worker_context, skill_id=skill.skill_id),
            tools=self._tools,
            llm=self._llm,
            checkpointer=self._checkpointer,
            instruction_resolver=lambda ref: skill.get_instruction(ref),
            intent_resolver=self._intent_for,
            budget=budget,
            tenant_id=worker_context.tenant_id,
            worker_id=worker_context.worker_id,
            skill_id=skill.skill_id,
            thread_id=thread_id,
            available_tools=tuple(available_tools or ()),
        )

    def _intent_for(self, ref: str) -> LLMCallIntent:
        name = str(ref or "").strip().lower()
        if "risk" in name or "class" in name or "route" in name:
            return LLMCallIntent(purpose=Purpose.CLASSIFY)
        return LLMCallIntent(purpose=Purpose.GENERATE)

    def _config_for(
        self,
        *,
        thread_id: str,
        tenant_id: str,
        worker_id: str,
        skill: Skill,
        whitelist: tuple[str, ...],
    ) -> dict[str, Any]:
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
                "tenant_id": tenant_id,
                "worker_id": worker_id,
                "skill_id": skill.skill_id,
                "source_path": skill.source_path,
                "state_whitelist": list(whitelist),
            }
        }

    async def _run_stream(
        self,
        *,
        bundle: CompiledGraphBundle,
        stream,
        run_id: str,
    ) -> AsyncGenerator[StreamEvent, None]:
        async for raw_event in stream:
            if self._is_interrupt_event(raw_event):
                continue
            mapped = self._stream_adapter._map_event(raw_event, run_id)
            if mapped is not None:
                yield mapped

    async def _emit_pause_if_needed(
        self,
        *,
        bundle: CompiledGraphBundle,
        config: dict[str, Any],
        run_id: str,
        tenant_id: str,
        worker_id: str,
        skill: Skill,
    ) -> ApprovalPendingEvent | None:
        snapshot = await bundle.compiled.aget_state(config)
        next_nodes = tuple(getattr(snapshot, "next", ()) or ())
        if not next_nodes:
            return None
        node_name = str(next_nodes[0])
        node = bundle.interrupt_nodes.get(node_name)
        if node is None:
            return None
        state = dict(getattr(snapshot, "values", {}) or {})
        await self._checkpointer.register_thread(
            thread_id=run_id,
            tenant_id=tenant_id,
            worker_id=worker_id,
            skill_id=skill.skill_id,
            source_path=skill.source_path,
        )
        prompt_template = skill.get_instruction(node.prompt_ref or node.instruction_ref or "general")
        inbox_id, prompt = await self._interrupt_bridge.create_inbox(
            tenant_id=tenant_id,
            worker_id=worker_id,
            thread_id=run_id,
            skill_id=skill.skill_id,
            node=node,
            state=state,
            state_whitelist=bundle.state_whitelist,
            prompt_template=prompt_template,
        )
        await bundle.compiled.aupdate_state(
            config,
            {"_approval_inbox_id": inbox_id},
            as_node=node.name,
        )
        state_digest = compute_state_digest(state, bundle.state_whitelist)
        await self._checkpointer.annotate_thread(
            thread_id=run_id,
            state_digest=state_digest,
            whitelist=bundle.state_whitelist,
        )
        return ApprovalPendingEvent(
            run_id=run_id,
            thread_id=run_id,
            inbox_id=inbox_id,
            prompt=prompt,
        )

    async def _emit_final_state_texts(
        self,
        *,
        bundle: CompiledGraphBundle,
        config: dict[str, Any],
        run_id: str,
    ) -> AsyncGenerator[TextMessageEvent, None]:
        graph = bundle.skill.strategy.graph
        if graph is None:
            return
        snapshot = await bundle.compiled.aget_state(config)
        state = dict(getattr(snapshot, "values", {}) or {})
        seen: set[str] = set()
        if graph.source == "yaml":
            for node in graph.nodes:
                if node.kind.value != "llm":
                    continue
                content = str(state.get(node.name, "") or "").strip()
                if content and content not in seen:
                    seen.add(content)
                    yield TextMessageEvent(run_id=run_id, content=content)
            return
        if graph.source == "python":
            for key, value in state.items():
                if key in {"task", "input"}:
                    continue
                content = str(value or "").strip()
                if content and content not in seen:
                    seen.add(content)
                    yield TextMessageEvent(run_id=run_id, content=content)

    def _load_skill(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        skill_id: str,
        source_path: str = "",
    ) -> Skill | None:
        if source_path:
            path = Path(source_path)
            if path.is_file():
                try:
                    return self._skill_loader._parser.parse(path)
                except Exception:
                    logger.warning("[LangGraphEngine] Failed to load skill from %s", path, exc_info=True)
        candidates = (
            self._workspace_root / "system" / "skills",
            self._workspace_root / "tenants" / tenant_id / "workers" / worker_id / "skills",
        )
        for skill in self._skill_loader.scan_multiple(candidates):
            if skill.skill_id == skill_id:
                return skill
        return None

    def _initial_state(self, task: str, whitelist: tuple[str, ...]) -> dict[str, Any]:
        state: dict[str, Any] = {"task": task, "input": task}
        if len(whitelist) == 1 and whitelist[0] not in state:
            state[whitelist[0]] = task
        for key in whitelist:
            if key.endswith("_id") and key not in state:
                state[key] = task.strip()
        return state

    def _is_interrupt_event(self, raw_event: dict[str, Any]) -> bool:
        data = raw_event.get("data", {})
        chunk = data.get("chunk")
        return isinstance(chunk, dict) and "__interrupt__" in chunk
