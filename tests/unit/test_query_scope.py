# edition: baseline
"""
Unit tests for QueryScope - SQL validation, forbidden tables/ops, and injection.
"""
import pytest

from src.worker.data_access.models import QueryDimension, QueryPolicy
from src.worker.data_access.query_scope import (
    QueryCheckResult,
    check_and_inject_sql,
    inject_where_condition,
)


def _policy(
    auto_inject: tuple[QueryDimension, ...] = (),
    forbidden_tables: tuple[str, ...] = (),
    forbidden_operations: tuple[str, ...] = (),
) -> QueryPolicy:
    return QueryPolicy(
        auto_inject=auto_inject,
        forbidden_tables=forbidden_tables,
        forbidden_operations=forbidden_operations,
    )


class TestForbiddenOperations:
    """Tests for forbidden operation detection."""

    def test_insert_blocked(self) -> None:
        policy = _policy(forbidden_operations=("INSERT",))
        result = check_and_inject_sql(
            "INSERT INTO users VALUES (1, 'a')", policy,
        )
        assert result.allowed is False
        assert "Forbidden operation" in result.rejection_reason

    def test_delete_blocked(self) -> None:
        policy = _policy(forbidden_operations=("DELETE",))
        result = check_and_inject_sql("DELETE FROM users WHERE id = 1", policy)
        assert result.allowed is False

    def test_drop_blocked(self) -> None:
        policy = _policy(forbidden_operations=("DROP",))
        result = check_and_inject_sql("DROP TABLE users", policy)
        assert result.allowed is False

    def test_select_allowed_when_write_forbidden(self) -> None:
        policy = _policy(
            forbidden_operations=("INSERT", "DELETE", "DROP", "UPDATE"),
        )
        result = check_and_inject_sql("SELECT * FROM users", policy)
        assert result.allowed is True


class TestForbiddenTables:
    """Tests for forbidden table detection with wildcard support."""

    def test_system_wildcard_blocks_system_tables(self) -> None:
        policy = _policy(forbidden_tables=("system_*",))
        result = check_and_inject_sql(
            "SELECT * FROM system_config", policy,
        )
        assert result.allowed is False
        assert "system_config" in result.rejection_reason

    def test_system_wildcard_blocks_system_logs(self) -> None:
        policy = _policy(forbidden_tables=("system_*",))
        result = check_and_inject_sql(
            "SELECT * FROM system_logs WHERE id = 1", policy,
        )
        assert result.allowed is False

    def test_exact_table_name_blocked(self) -> None:
        policy = _policy(forbidden_tables=("secrets",))
        result = check_and_inject_sql("SELECT * FROM secrets", policy)
        assert result.allowed is False

    def test_normal_table_allowed(self) -> None:
        policy = _policy(forbidden_tables=("system_*",))
        result = check_and_inject_sql("SELECT * FROM orders", policy)
        assert result.allowed is True


class TestWhereInjection:
    """Tests for auto_inject WHERE condition injection."""

    def test_inject_where_when_missing(self) -> None:
        policy = _policy(
            auto_inject=(QueryDimension(column="tenant_id", value="t1"),),
        )
        result = check_and_inject_sql("SELECT * FROM orders", policy)
        assert result.allowed is True
        assert "tenant_id = 't1'" in result.modified_sql
        assert "WHERE" in result.modified_sql

    def test_inject_appends_and_when_where_exists(self) -> None:
        policy = _policy(
            auto_inject=(QueryDimension(column="tenant_id", value="t1"),),
        )
        sql = "SELECT * FROM orders WHERE status = 'active'"
        result = check_and_inject_sql(sql, policy)
        assert result.allowed is True
        assert "AND tenant_id = 't1'" in result.modified_sql

    def test_existing_correct_value_not_duplicated(self) -> None:
        policy = _policy(
            auto_inject=(QueryDimension(column="tenant_id", value="t1"),),
        )
        sql = "SELECT * FROM orders WHERE tenant_id = 't1'"
        result = check_and_inject_sql(sql, policy)
        assert result.allowed is True

    def test_existing_wrong_value_rejected(self) -> None:
        policy = _policy(
            auto_inject=(QueryDimension(column="tenant_id", value="t1"),),
        )
        sql = "SELECT * FROM orders WHERE tenant_id = 't999'"
        result = check_and_inject_sql(sql, policy)
        assert result.allowed is False
        assert "Condition conflict" in result.rejection_reason

    def test_inject_before_order_by(self) -> None:
        policy = _policy(
            auto_inject=(QueryDimension(column="tenant_id", value="t1"),),
        )
        sql = "SELECT * FROM orders ORDER BY created_at"
        result = check_and_inject_sql(sql, policy)
        assert result.allowed is True
        assert result.modified_sql.index("WHERE") < result.modified_sql.index("ORDER")

    def test_inject_before_limit(self) -> None:
        policy = _policy(
            auto_inject=(QueryDimension(column="tenant_id", value="t1"),),
        )
        sql = "SELECT * FROM orders LIMIT 10"
        result = check_and_inject_sql(sql, policy)
        assert result.allowed is True
        assert result.modified_sql.index("WHERE") < result.modified_sql.index("LIMIT")

    def test_inject_before_group_by(self) -> None:
        policy = _policy(
            auto_inject=(QueryDimension(column="tenant_id", value="t1"),),
        )
        sql = "SELECT status, count(*) FROM orders GROUP BY status"
        result = check_and_inject_sql(sql, policy)
        assert result.allowed is True
        assert result.modified_sql.index("WHERE") < result.modified_sql.index("GROUP")


class TestInjectWhereCondition:
    """Tests for inject_where_condition pure function."""

    def test_no_where_appends(self) -> None:
        result = inject_where_condition(
            "SELECT * FROM t", "col", "val",
        )
        assert result == "SELECT * FROM t WHERE col = 'val'"

    def test_existing_where_appends_and(self) -> None:
        result = inject_where_condition(
            "SELECT * FROM t WHERE a = 1", "col", "val",
        )
        assert "AND col = 'val'" in result

    def test_combined_operations_and_injection(self) -> None:
        policy = _policy(
            auto_inject=(QueryDimension(column="tenant_id", value="t1"),),
            forbidden_operations=("DELETE",),
            forbidden_tables=("system_*",),
        )
        result = check_and_inject_sql(
            "SELECT * FROM orders", policy,
        )
        assert result.allowed is True
        assert "tenant_id = 't1'" in result.modified_sql
