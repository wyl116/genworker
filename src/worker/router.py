"""
WorkerRouter - unified routing entry point.

Flow: find worker -> build context -> match skill -> dispatch engine -> create task manifest.
route_stream() is an async generator yielding StreamEvent.
"""
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from src.common.logger import get_logger
from src.common.paths import resolve_workspace_root
from src.common.tenant import Tenant, TenantLoader
from src.engine.state import UsageBudget
from src.memory.orchestrator import MemoryOrchestrator
from src.skills.matcher import MatchStatus, SkillMatcher
from src.streaming.events import ErrorEvent, StreamEvent
from src.tools.runtime_scope import ExecutionScopeProvider

from .contacts.context import format_contacts_markdown, select_contacts_for_context
from .registry import WorkerRegistry
from .runtime_context import WorkerRuntimeContextBuilder
from .task_runner import TaskRunner
from .task import TaskManifest
from .tool_scope import build_tool_runtime_bundle, with_skill_scope
from .trust_gate import compute_trust_gate

# Optional SubAgent executor for Coordinator pattern
try:
    from src.engine.tools.subagent_tool import create_spawn_subagents_tool
    _SUBAGENT_AVAILABLE = True
except ImportError:
    _SUBAGENT_AVAILABLE = False

logger = get_logger()


class WorkerRouter:
    """
    Unified routing entry point for worker task execution.

    Orchestrates: Worker lookup -> Context build -> Skill match
    -> Engine dispatch via TaskRunner.
    """

    def __init__(
        self,
        worker_registry: WorkerRegistry,
        tenant_loader: TenantLoader,
        task_runner: TaskRunner,
        all_tools: tuple[Any, ...] = (),
        mcp_server: object | None = None,
        workspace_root: Path | None = None,
        subagent_executor: Any | None = None,
        contact_registries: dict[str, Any] | None = None,
        memory_orchestrator: MemoryOrchestrator | None = None,
        execution_scope_provider: ExecutionScopeProvider | None = None,
        session_search_index: Any | None = None,
        task_spawner: Any | None = None,
    ) -> None:
        self._worker_registry = worker_registry
        self._tenant_loader = tenant_loader
        self._task_runner = task_runner
        self._all_tools = all_tools
        self._mcp_server = mcp_server
        self._workspace_root = resolve_workspace_root(workspace_root)
        self._subagent_executor = subagent_executor
        self._contact_registries = contact_registries or {}
        self._memory_orchestrator = memory_orchestrator
        self._execution_scope_provider = execution_scope_provider or ExecutionScopeProvider()
        self._session_search_index = session_search_index
        self._task_spawner = task_spawner
        self._runtime_context_builder = WorkerRuntimeContextBuilder(
            workspace_root=self._workspace_root,
            memory_orchestrator=self._memory_orchestrator,
        )

    async def route_stream(
        self,
        task: str,
        tenant_id: str,
        worker_id: Optional[str] = None,
        skill_id: str | None = None,
        preferred_skill_ids: tuple[str, ...] | None = None,
        task_context: str = "",
        budget: UsageBudget | None = None,
        manifest: TaskManifest | None = None,
        tool_whitelist: tuple[str, ...] | None = None,
        subagent_depth: int = 0,
        max_rounds_override: int | None = None,
        conversation_session: Any | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """
        Route a task through the full processing pipeline.

        Steps:
        1. Load tenant
        2. Find worker (by ID or match)
        3. Compute trust gate
        4. Compute tool sandbox
        5. Build worker context
        6. Match skill
        7. Dispatch engine via TaskRunner

        Args:
            task: User task description.
            tenant_id: Tenant identifier.
            worker_id: Optional specific worker ID.
            budget: Optional token budget.

        Yields:
            StreamEvent objects.
        """
        # 1. Load tenant
        try:
            tenant = self._tenant_loader.load(tenant_id)
        except Exception as exc:
            logger.error(f"[WorkerRouter] Failed to load tenant '{tenant_id}': {exc}")
            yield ErrorEvent(
                run_id="",
                code="TENANT_NOT_FOUND",
                message=f"Tenant '{tenant_id}' not found: {exc}",
            )
            return

        # 2. Find worker
        entry = self._find_worker(task, tenant, worker_id)
        if entry is None:
            yield ErrorEvent(
                run_id="",
                code="WORKER_NOT_FOUND",
                message=f"No worker found for tenant '{tenant_id}'",
            )
            return

        worker = entry.worker
        contact_context = ""
        if not worker.is_service:
            registry = self._contact_registries.get(worker.worker_id)
            selected_contacts = select_contacts_for_context(registry, query=task)
            contact_context = format_contacts_markdown(selected_contacts)

        # 3. Compute trust gate
        trust_gate = compute_trust_gate(worker, tenant)

        # 4. Match skill first so scoped rules can be filtered correctly
        requested_skill_id = skill_id or getattr(manifest, "skill_id", "") or ""
        requested_preferred_skill_ids = preferred_skill_ids
        if requested_preferred_skill_ids is None:
            requested_preferred_skill_ids = getattr(
                manifest,
                "preferred_skill_ids",
                (),
            ) or ()
        if requested_skill_id:
            skill = entry.skill_registry.get(requested_skill_id)
            if skill is None:
                yield ErrorEvent(
                    run_id="",
                    code="SKILL_NOT_FOUND",
                    message=f"Skill '{requested_skill_id}' not found.",
                )
                return
        else:
            skill_matcher = SkillMatcher(registry=entry.skill_registry)
            match_result = await skill_matcher.match(
                task,
                preferred_skill_ids=requested_preferred_skill_ids,
            )
            if match_result.status == MatchStatus.NOT_FOUND:
                if worker.default_skill:
                    skill = entry.skill_registry.get(worker.default_skill)
                    if skill is None:
                        yield ErrorEvent(
                            run_id="",
                            code="SKILL_NOT_FOUND",
                            message="No matching skill found. Please rephrase your request.",
                        )
                        return
                else:
                    yield ErrorEvent(
                        run_id="",
                        code="SKILL_NOT_FOUND",
                        message="No matching skill found. Please rephrase your request.",
                    )
                    return
            else:
                skill = match_result.skill

        # 5. Compute tool runtime
        all_tools = (
            tuple(self._mcp_server.get_all_tools())
            if self._mcp_server else self._all_tools
        )
        tool_bundle = build_tool_runtime_bundle(
            worker=worker,
            tenant=tenant,
            trust_gate=trust_gate,
            all_tools=all_tools,
            worker_router=self,
            subagent_executor=(
                self._subagent_executor if _SUBAGENT_AVAILABLE else None
            ),
            create_subagent_tool_fn=(
                create_spawn_subagents_tool if _SUBAGENT_AVAILABLE else None
            ),
            task_spawner=self._task_spawner,
            conversation_session=conversation_session,
            session_search_index=self._session_search_index,
            tool_whitelist=tool_whitelist,
            subagent_depth=subagent_depth,
            parent_task_id=task[:64],
        )

        # 6. Build worker context (with Phase 7 injections)
        runtime_context = await self._runtime_context_builder.build(
            worker=worker,
            tenant=tenant,
            trust_gate=trust_gate,
            skill=skill,
            available_tools=tool_bundle.available_tools,
            available_skill_ids=tuple(
                skill_item.skill_id for skill_item in entry.skill_registry.list_all()
            ),
            task=task,
            task_context=task_context,
            contact_context=contact_context,
            subagent_enabled=tool_bundle.subagent_enabled,
            provenance=getattr(manifest, "provenance", None),
        )

        # 7. Dispatch engine via TaskRunner
        scope = with_skill_scope(tool_bundle, skill_id=skill.skill_id)
        async with self._execution_scope_provider.use(scope):
            async for event in self._task_runner.execute(
                skill=skill,
                worker_context=runtime_context.worker_context,
                task=task,
                available_tools=tool_bundle.tool_schemas,
                budget=budget,
                manifest=manifest,
                applied_rule_ids=runtime_context.applied_rule_ids,
                max_rounds_override=max_rounds_override,
            ):
                yield event

    def _find_worker(
        self,
        task: str,
        tenant: Tenant,
        worker_id: Optional[str],
    ):
        """Find the appropriate worker entry."""
        if worker_id:
            entry = self._worker_registry.get(worker_id)
            if entry is not None:
                return entry
            # Explicit worker_id not found → return None (WORKER_NOT_FOUND)
            return None

        # No worker_id specified: try matching by task content
        entry = self._worker_registry.match(task)
        if entry is not None:
            return entry

        # Try tenant's default worker
        if tenant.default_worker:
            return self._worker_registry.get(tenant.default_worker)

        return None

    def resolve_entry(
        self,
        *,
        task: str,
        tenant_id: str,
        worker_id: Optional[str] = None,
    ):
        """Resolve the worker entry without running the execution pipeline."""
        tenant = self._tenant_loader.load(tenant_id)
        return self._find_worker(task, tenant, worker_id)

    def get_contact_registry(self, worker_id: str):
        """Return the contact registry for a worker if available."""
        return self._contact_registries.get(worker_id)

    def replace_worker_registry(self, worker_registry: WorkerRegistry) -> None:
        """Refresh the in-memory worker registry used for routing."""
        self._worker_registry = worker_registry
        if self._task_spawner is not None and hasattr(self._task_spawner, "replace_worker_registry"):
            self._task_spawner.replace_worker_registry(worker_registry)

    def set_session_search_index(self, search_index: Any | None) -> None:
        self._session_search_index = search_index

    def set_task_spawner(self, task_spawner: Any | None) -> None:
        self._task_spawner = task_spawner
