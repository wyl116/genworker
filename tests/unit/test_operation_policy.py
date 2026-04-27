# edition: baseline
"""
Unit tests for OperationPolicy - level determination and policy enforcement.
"""
import pytest

from src.worker.data_access.models import OperationLevel, OperationPolicyConfig
from src.worker.data_access.operation_policy import (
    determine_operation_level,
    enforce_operation_policy,
)


def _default_policy() -> OperationPolicyConfig:
    return OperationPolicyConfig(
        level_0_read=OperationLevel(policy="auto"),
        level_1_create=OperationLevel(policy="confirm"),
        level_2_modify=OperationLevel(policy="confirm", audit=True),
        level_3_delete=OperationLevel(
            policy="approval", audit=True, approval_target="admin",
        ),
    )


def _policy_with_overrides() -> OperationPolicyConfig:
    return OperationPolicyConfig(
        level_0_read=OperationLevel(policy="auto"),
        level_1_create=OperationLevel(policy="confirm"),
        level_2_modify=OperationLevel(policy="confirm", audit=True),
        level_3_delete=OperationLevel(policy="approval", audit=True),
        tool_overrides=(
            ("safe_delete", OperationLevel(policy="auto")),
            ("dangerous_read", OperationLevel(policy="approval")),
        ),
    )


def _policy_with_limits() -> OperationPolicyConfig:
    return OperationPolicyConfig(
        level_0_read=OperationLevel(policy="auto"),
        level_1_create=OperationLevel(policy="auto", daily_limit=5),
        level_2_modify=OperationLevel(policy="confirm", audit=True),
        level_3_delete=OperationLevel(policy="approval", audit=True),
    )


class TestDetermineOperationLevel:
    """Tests for determine_operation_level."""

    def test_read_maps_to_level_0(self) -> None:
        level_num, op_level = determine_operation_level(
            "query_tool", "READ", _default_policy(),
        )
        assert level_num == 0
        assert op_level.policy == "auto"

    def test_create_maps_to_level_1(self) -> None:
        level_num, op_level = determine_operation_level(
            "create_tool", "CREATE", _default_policy(),
        )
        assert level_num == 1
        assert op_level.policy == "confirm"

    def test_modify_maps_to_level_2(self) -> None:
        level_num, op_level = determine_operation_level(
            "update_tool", "MODIFY", _default_policy(),
        )
        assert level_num == 2
        assert op_level.policy == "confirm"
        assert op_level.audit is True

    def test_delete_maps_to_level_3(self) -> None:
        level_num, op_level = determine_operation_level(
            "delete_tool", "DELETE", _default_policy(),
        )
        assert level_num == 3
        assert op_level.policy == "approval"

    def test_tool_override_takes_precedence(self) -> None:
        level_num, op_level = determine_operation_level(
            "safe_delete", "DELETE", _policy_with_overrides(),
        )
        assert op_level.policy == "auto"

    def test_tool_override_dangerous_read(self) -> None:
        level_num, op_level = determine_operation_level(
            "dangerous_read", "READ", _policy_with_overrides(),
        )
        assert op_level.policy == "approval"

    def test_non_overridden_tool_uses_default(self) -> None:
        level_num, op_level = determine_operation_level(
            "normal_tool", "DELETE", _policy_with_overrides(),
        )
        assert op_level.policy == "approval"


class TestEnforceOperationPolicy:
    """Tests for enforce_operation_policy."""

    @pytest.mark.asyncio
    async def test_level_0_auto_approved(self) -> None:
        allowed, reason = await enforce_operation_policy(
            "query_tool", "READ", _default_policy(),
            worker_id="w1", tenant_id="t1", daily_usage={},
        )
        assert allowed is True
        assert "Auto-approved" in reason

    @pytest.mark.asyncio
    async def test_level_3_requires_approval(self) -> None:
        allowed, reason = await enforce_operation_policy(
            "delete_tool", "DELETE", _default_policy(),
            worker_id="w1", tenant_id="t1", daily_usage={},
        )
        assert allowed is False
        assert "Approval required" in reason

    @pytest.mark.asyncio
    async def test_level_1_confirm_passes(self) -> None:
        allowed, reason = await enforce_operation_policy(
            "create_tool", "CREATE", _default_policy(),
            worker_id="w1", tenant_id="t1", daily_usage={},
        )
        assert allowed is True
        assert "Confirmed" in reason

    @pytest.mark.asyncio
    async def test_daily_limit_exceeded(self) -> None:
        allowed, reason = await enforce_operation_policy(
            "create_tool", "CREATE", _policy_with_limits(),
            worker_id="w1", tenant_id="t1",
            daily_usage={"create_tool:CREATE": 5},
        )
        assert allowed is False
        assert "Daily limit exceeded" in reason

    @pytest.mark.asyncio
    async def test_daily_limit_not_exceeded(self) -> None:
        allowed, reason = await enforce_operation_policy(
            "create_tool", "CREATE", _policy_with_limits(),
            worker_id="w1", tenant_id="t1",
            daily_usage={"create_tool:CREATE": 3},
        )
        assert allowed is True

    @pytest.mark.asyncio
    async def test_override_auto_on_delete(self) -> None:
        allowed, reason = await enforce_operation_policy(
            "safe_delete", "DELETE", _policy_with_overrides(),
            worker_id="w1", tenant_id="t1", daily_usage={},
        )
        assert allowed is True
        assert "Auto-approved" in reason
