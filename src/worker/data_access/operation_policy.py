"""
OperationPolicy - operation level determination and policy enforcement.

Pure functions for mapping operations to levels and enforcing policies.
"""
from src.worker.data_access.models import OperationLevel, OperationPolicyConfig


_OPERATION_TYPE_LEVELS: dict[str, int] = {
    "READ": 0,
    "CREATE": 1,
    "MODIFY": 2,
    "DELETE": 3,
}


class OperationDeniedError(Exception):
    """Raised when an operation is denied by policy."""


def determine_operation_level(
    tool_name: str,
    operation_type: str,
    policy: OperationPolicyConfig,
) -> tuple[int, OperationLevel]:
    """
    Pure function: resolve operation level for a tool.

    Priority: tool_overrides first, then default level mapping by operation type.
    """
    override = _find_tool_override(tool_name, policy.tool_overrides)
    if override is not None:
        level_num = _OPERATION_TYPE_LEVELS.get(operation_type.upper(), 0)
        return (level_num, override)

    return _default_level_for_operation(operation_type, policy)


async def enforce_operation_policy(
    tool_name: str,
    operation_type: str,
    policy: OperationPolicyConfig,
    worker_id: str,
    tenant_id: str,
    daily_usage: dict[str, int],
) -> tuple[bool, str]:
    """
    Enforce operation policy: daily limit check, then policy-based decision.

    Returns (allowed, reason).
    """
    level_num, op_level = determine_operation_level(
        tool_name, operation_type, policy,
    )
    usage_key = f"{tool_name}:{operation_type}"
    current_usage = daily_usage.get(usage_key, 0)

    if op_level.daily_limit is not None and current_usage >= op_level.daily_limit:
        return (
            False,
            f"Daily limit exceeded for {usage_key}: "
            f"{current_usage}/{op_level.daily_limit}",
        )

    return _apply_policy(op_level.policy, level_num, tool_name, operation_type)


def _find_tool_override(
    tool_name: str,
    overrides: tuple[tuple[str, OperationLevel], ...],
) -> OperationLevel | None:
    """Find override for a specific tool name."""
    for name, level in overrides:
        if name == tool_name:
            return level
    return None


def _default_level_for_operation(
    operation_type: str,
    policy: OperationPolicyConfig,
) -> tuple[int, OperationLevel]:
    """Map operation type to its default level config."""
    op_upper = operation_type.upper()
    level_map: dict[str, tuple[int, OperationLevel]] = {
        "READ": (0, policy.level_0_read),
        "CREATE": (1, policy.level_1_create),
        "MODIFY": (2, policy.level_2_modify),
        "DELETE": (3, policy.level_3_delete),
    }
    return level_map.get(op_upper, (0, policy.level_0_read))


def _apply_policy(
    policy_str: str,
    level_num: int,
    tool_name: str,
    operation_type: str,
) -> tuple[bool, str]:
    """Apply policy string to determine allow/deny."""
    if policy_str == "auto":
        return (True, f"Auto-approved: level {level_num} ({operation_type})")

    if policy_str == "confirm":
        return (True, f"Confirmed: level {level_num} ({operation_type})")

    if policy_str == "approval":
        return (
            False,
            f"Approval required: level {level_num} ({operation_type}) "
            f"for tool '{tool_name}'",
        )

    return (False, f"Unknown policy: {policy_str}")
