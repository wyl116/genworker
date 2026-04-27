"""
QueryScope - SQL query scoping with condition injection and validation.

Pure functions for:
- Forbidden operation checking (INSERT, DELETE, DROP, etc.)
- Forbidden table checking (supports wildcards like system_*)
- WHERE condition auto-injection
"""
import re
from dataclasses import dataclass

from src.worker.data_access.models import QueryPolicy


@dataclass(frozen=True)
class QueryCheckResult:
    """Result of SQL check and injection."""
    allowed: bool
    modified_sql: str | None = None
    rejection_reason: str | None = None


def check_and_inject_sql(sql: str, policy: QueryPolicy) -> QueryCheckResult:
    """
    Pure function: validate SQL against policy, then inject WHERE conditions.

    1. Check forbidden operations (INSERT/DELETE/DROP etc.)
    2. Check forbidden tables (supports wildcard like system_*)
    3. Inject WHERE conditions for auto_inject dimensions
    """
    rejection = _check_forbidden_operations(sql, policy.forbidden_operations)
    if rejection:
        return QueryCheckResult(allowed=False, rejection_reason=rejection)

    rejection = _check_forbidden_tables(sql, policy.forbidden_tables)
    if rejection:
        return QueryCheckResult(allowed=False, rejection_reason=rejection)

    modified = sql
    for dim in policy.auto_inject:
        conflict = _check_existing_condition(modified, dim.column, dim.value)
        if conflict:
            return QueryCheckResult(allowed=False, rejection_reason=conflict)
        if not _has_condition(modified, dim.column):
            modified = inject_where_condition(modified, dim.column, dim.value)

    return QueryCheckResult(allowed=True, modified_sql=modified)


def inject_where_condition(sql: str, column: str, value: str) -> str:
    """
    Pure function: inject a WHERE condition into SQL.

    - Has WHERE -> append AND column = 'value'
    - No WHERE -> insert before GROUP BY/ORDER BY/LIMIT or at end
    """
    condition = f"{column} = '{value}'"

    if _has_where_clause(sql):
        return _append_and_condition(sql, condition)
    return _insert_where_clause(sql, condition)


def _check_forbidden_operations(
    sql: str, forbidden: tuple[str, ...],
) -> str | None:
    """Return rejection reason if SQL contains a forbidden operation."""
    sql_upper = sql.upper().strip()
    for op in forbidden:
        pattern = rf'\b{re.escape(op.upper())}\b'
        if re.search(pattern, sql_upper):
            return f"Forbidden operation: {op}"
    return None


def _check_forbidden_tables(
    sql: str, forbidden: tuple[str, ...],
) -> str | None:
    """Return rejection reason if SQL references a forbidden table."""
    sql_lower = sql.lower()
    for table_pattern in forbidden:
        if table_pattern.endswith("*"):
            prefix = table_pattern[:-1].lower()
            matches = re.findall(r'\b(\w+)\b', sql_lower)
            for word in matches:
                if word.startswith(prefix) and word != prefix.rstrip("_"):
                    return f"Forbidden table: {word} (matches {table_pattern})"
        else:
            pattern = rf'\b{re.escape(table_pattern.lower())}\b'
            if re.search(pattern, sql_lower):
                return f"Forbidden table: {table_pattern}"
    return None


def _check_existing_condition(
    sql: str, column: str, expected_value: str,
) -> str | None:
    """If column already appears in WHERE with a different value, return reason."""
    pattern = rf"{re.escape(column)}\s*=\s*'([^']*)'"
    match = re.search(pattern, sql, re.IGNORECASE)
    if match and match.group(1) != expected_value:
        return (
            f"Condition conflict: {column} = '{match.group(1)}' "
            f"but policy requires '{expected_value}'"
        )
    return None


def _has_condition(sql: str, column: str) -> bool:
    """Check if column already appears in a WHERE condition."""
    pattern = rf"{re.escape(column)}\s*="
    return bool(re.search(pattern, sql, re.IGNORECASE))


def _has_where_clause(sql: str) -> bool:
    """Check if SQL has a WHERE clause."""
    return bool(re.search(r'\bWHERE\b', sql, re.IGNORECASE))


def _append_and_condition(sql: str, condition: str) -> str:
    """Append AND condition after existing WHERE clause."""
    # Insert before GROUP BY/ORDER BY/LIMIT if present
    tail_pattern = re.compile(
        r'(\s+(?:GROUP\s+BY|ORDER\s+BY|LIMIT)\b.*)',
        re.IGNORECASE | re.DOTALL,
    )
    match = tail_pattern.search(sql)
    if match:
        insert_pos = match.start()
        return f"{sql[:insert_pos]} AND {condition}{sql[insert_pos:]}"
    return f"{sql} AND {condition}"


def _insert_where_clause(sql: str, condition: str) -> str:
    """Insert WHERE clause before GROUP BY/ORDER BY/LIMIT or at end."""
    tail_pattern = re.compile(
        r'(\s+(?:GROUP\s+BY|ORDER\s+BY|LIMIT)\b.*)',
        re.IGNORECASE | re.DOTALL,
    )
    match = tail_pattern.search(sql)
    if match:
        insert_pos = match.start()
        return f"{sql[:insert_pos]} WHERE {condition}{sql[insert_pos:]}"
    return f"{sql} WHERE {condition}"
