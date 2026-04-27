"""
ToolPipeline - Three-layer tool execution architecture.

Layer 1: Pre-hooks (BashSecurityHook, etc.) - allow/deny/warn, deny short-circuits
Layer 2: Middleware chain (Permission, SchemaValidation, Timeout)
Layer 3: ScopedToolExecutor terminal executor
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from src.common.logger import get_logger
from src.tools.security.audit import AuditLogger
from src.tools.security.models import PolicyDecision
from src.tools.security.policy import PolicyEvaluator

from .formatters import ToolResult
from .hooks import HookAction, ToolHook
from .runtime_scope import use_tool_pipeline
from .sandbox import ScopedToolExecutor

logger = get_logger()


@dataclass(frozen=True)
class ToolCallContext:
    """Immutable context for a single tool call through the pipeline."""
    worker_id: str
    tenant_id: str
    skill_id: str
    step_name: str | None
    tool_name: str
    tool_input: dict[str, Any]
    risk_level: str
    tool: Any | None = None
    execution_scope: Any | None = None
    constraint: Any | None = None

    @classmethod
    def from_scope(
        cls,
        scope: Any,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        risk_level: str,
        tool: Any | None,
        step_name: str | None = None,
    ) -> "ToolCallContext":
        return cls(
            worker_id=str(getattr(scope, "worker_id", "")),
            tenant_id=str(getattr(scope, "tenant_id", "")),
            skill_id=str(getattr(scope, "skill_id", "")),
            step_name=step_name,
            tool_name=tool_name,
            tool_input=tool_input,
            risk_level=risk_level,
            tool=tool,
            execution_scope=scope,
            constraint=getattr(scope, "constraint", None),
        )


class ToolMiddleware(Protocol):
    """Protocol for pipeline middleware."""

    async def process(
        self, ctx: ToolCallContext, next_fn: Callable[[], Any]
    ) -> ToolResult:
        """
        Process the tool call, optionally delegating to next_fn.

        Args:
            ctx: Tool call context.
            next_fn: Callable to invoke the next middleware or executor.

        Returns:
            ToolResult from downstream or a short-circuit result.
        """
        ...


class ToolPipeline:
    """
    Three-layer tool execution pipeline.

    Hooks -> Middleware chain -> ScopedToolExecutor.
    """

    def __init__(
        self,
        executor: ScopedToolExecutor,
        middlewares: Sequence[ToolMiddleware] = (),
        hooks: Sequence[ToolHook] = (),
        policy: PolicyEvaluator | None = None,
        fences: Sequence[Any] = (),
        audit_logger: AuditLogger | None = None,
    ):
        self._executor = executor
        self._middlewares = tuple(middlewares)
        self._hooks = tuple(hooks)
        self._policy = policy or PolicyEvaluator()
        self._fences = tuple(fences)
        self._audit_logger = audit_logger or AuditLogger()

    @property
    def executor(self) -> ScopedToolExecutor:
        return self._executor

    @property
    def middlewares(self) -> tuple:
        return self._middlewares

    @property
    def hooks(self) -> tuple:
        return self._hooks

    async def execute(self, ctx: ToolCallContext) -> ToolResult:
        """
        Execute a tool call through the full pipeline.

        1. Run pre-hooks (deny short-circuits)
        2. Run middleware chain -> ScopedToolExecutor
        3. Run post-hooks
        """
        async with use_tool_pipeline(self):
            policy_result = self._policy.evaluate(ctx)
            if policy_result.decision != PolicyDecision.ALLOW:
                self._audit_logger.log(
                    tenant_id=ctx.tenant_id,
                    worker_id=ctx.worker_id,
                    tool_name=ctx.tool_name,
                    policy_decision=policy_result.decision.value,
                    enforcement_result="skipped",
                    error_message=policy_result.reason,
                )
                return ToolResult(content=policy_result.reason, is_error=True)

            for fence in self._fences:
                allowed, reason = fence.check(ctx)
                if not allowed:
                    self._audit_logger.log(
                        tenant_id=ctx.tenant_id,
                        worker_id=ctx.worker_id,
                        tool_name=ctx.tool_name,
                        policy_decision=policy_result.decision.value,
                        enforcement_result="blocked",
                        error_message=reason,
                    )
                    return ToolResult(content=reason, is_error=True)

            # 1. Pre-hooks
            for hook in self._hooks:
                hook_result = await hook.pre_execute(ctx.tool_name, ctx.tool_input)
                if hook_result.action == HookAction.DENY:
                    logger.warning(
                        f"[ToolPipeline] Hook denied tool '{ctx.tool_name}': "
                        f"{hook_result.message}"
                    )
                    return ToolResult(content=hook_result.message, is_error=True)
                if hook_result.action == HookAction.WARN:
                    logger.warning(
                        f"[ToolPipeline] Hook warning for '{ctx.tool_name}': "
                        f"{hook_result.message}"
                    )

            # 2. Middleware chain -> ScopedToolExecutor
            result = await self._run_chain(ctx, 0)

            # 3. Post-hooks
            for hook in self._hooks:
                await hook.post_execute(ctx.tool_name, ctx.tool_input, result)

            self._audit_logger.log(
                tenant_id=ctx.tenant_id,
                worker_id=ctx.worker_id,
                tool_name=ctx.tool_name,
                policy_decision=policy_result.decision.value,
                enforcement_result="allowed",
                error_message=result.content if result.is_error else "",
            )
            return result

    async def _run_chain(self, ctx: ToolCallContext, index: int) -> ToolResult:
        """Recursively run middleware chain, terminating at executor."""
        if index >= len(self._middlewares):
            return await self._executor.execute(
                ctx.tool_name,
                ctx.tool_input,
                tool=ctx.tool,
            )
        return await self._middlewares[index].process(
            ctx, lambda: self._run_chain(ctx, index + 1)
        )
