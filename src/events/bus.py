"""
EventBus - in-process publish/subscribe with tenant isolation.

Features:
- Tenant-scoped namespace isolation
- Wildcard subscriptions ("data.*" matches "data.file_uploaded")
- Payload filter matching (AND semantics, with exact/regex/expression support)
- Async non-blocking dispatch
"""
import logging
import re
from typing import Any

from .models import Event, EventHandler, Subscription

logger = logging.getLogger(__name__)

def _event_type_matches(pattern: str, event_type: str) -> bool:
    """
    Check if an event type matches a subscription pattern.

    Rules:
    - "*" matches everything
    - "data.*" matches "data.xxx" (single level wildcard)
    - Exact match for everything else
    """
    if pattern == "*":
        return True
    if pattern == event_type:
        return True
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        if event_type.startswith(prefix + "."):
            # Only match one level: "data.*" matches "data.x" but not "data.x.y"
            remainder = event_type[len(prefix) + 1:]
            return len(remainder) > 0 and "." not in remainder
    return False


def _filter_matches(
    filter_spec: tuple[tuple[str, str], ...],
    payload: tuple[tuple[str, Any], ...],
) -> bool:
    """
    Check if all filter conditions match the payload (AND semantics).

    Empty filter always matches. Each filter key must exist in payload
    with a matching value. Supports list values in payload (any match).
    """
    if not filter_spec:
        return True

    payload_dict: dict[str, Any] = dict(payload)

    for key, expected_value in filter_spec:
        actual = payload_dict.get(key)
        if actual is None:
            return False
        if not _match_filter_value(actual, expected_value):
            return False

    return True


def _match_filter_value(actual: Any, expected_value: str) -> bool:
    """
    Match one payload value against a filter expression.

    Supported filter formats:
    - exact match: "csv"
    - regex: "regex:^report-\\d+$"
    - contains: "contains:urgent"
    - startswith: "startswith:prod-"
    - endswith: "endswith:.pdf"
    - numeric comparisons: ">= 3", "< 0.1", "between 1 and 5"
    """
    if isinstance(actual, (list, tuple)):
        return any(_match_filter_value(item, expected_value) for item in actual)

    expected = str(expected_value)
    actual_text = str(actual)
    lowered = expected.lower()

    if lowered.startswith("regex:"):
        pattern = expected[len("regex:"):]
        return bool(re.search(pattern, actual_text, re.IGNORECASE))

    if lowered.startswith("contains:"):
        needle = expected[len("contains:"):]
        return needle.lower() in actual_text.lower()

    if lowered.startswith("startswith:"):
        prefix = expected[len("startswith:"):]
        return actual_text.lower().startswith(prefix.lower())

    if lowered.startswith("endswith:"):
        suffix = expected[len("endswith:"):]
        return actual_text.lower().endswith(suffix.lower())

    expression_match = _match_scalar_expression(actual, expected)
    if expression_match is not None:
        return expression_match

    return actual_text == expected


def _match_scalar_expression(actual: Any, expression: str) -> bool | None:
    """Evaluate a simple scalar expression, returning None when unsupported."""
    expr = str(expression or "").strip()
    if not expr:
        return None

    between_match = re.fullmatch(
        r"between\s+(-?\d+(?:\.\d+)?)\s+and\s+(-?\d+(?:\.\d+)?)",
        expr,
        flags=re.IGNORECASE,
    )
    if between_match:
        actual_num = _to_float(actual)
        if actual_num is None:
            return False
        lower = float(between_match.group(1))
        upper = float(between_match.group(2))
        return lower <= actual_num <= upper

    compare_match = re.fullmatch(
        r"(>=|<=|>|<|==|!=)\s*(-?\d+(?:\.\d+)?)",
        expr,
    )
    if compare_match:
        actual_num = _to_float(actual)
        if actual_num is None:
            return False
        operator, raw_value = compare_match.groups()
        expected_num = float(raw_value)
        if operator == ">":
            return actual_num > expected_num
        if operator == ">=":
            return actual_num >= expected_num
        if operator == "<":
            return actual_num < expected_num
        if operator == "<=":
            return actual_num <= expected_num
        if operator == "==":
            return actual_num == expected_num
        return actual_num != expected_num

    return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class EventBus:
    """
    In-process publish/subscribe EventBus with tenant isolation.

    Subscriptions are organized by tenant_id for namespace isolation.
    """

    def __init__(self) -> None:
        # tenant_id -> list of Subscription
        self._subscriptions: dict[str, list[Subscription]] = {}

    def subscribe(self, subscription: Subscription) -> str:
        """
        Register an event handler, scoped to its tenant_id.

        Returns the handler_id for later unsubscription.
        """
        tenant_subs = self._subscriptions.setdefault(
            subscription.tenant_id, []
        )
        tenant_subs.append(subscription)
        logger.debug(
            f"[EventBus] Subscribed {subscription.handler_id} "
            f"to '{subscription.event_type}' for tenant '{subscription.tenant_id}'"
        )
        return subscription.handler_id

    def unsubscribe(self, tenant_id: str, handler_id: str) -> bool:
        """
        Remove a subscription by tenant_id and handler_id.

        Returns True if found and removed, False otherwise.
        """
        tenant_subs = self._subscriptions.get(tenant_id)
        if tenant_subs is None:
            return False

        original_len = len(tenant_subs)
        updated = [s for s in tenant_subs if s.handler_id != handler_id]

        if len(updated) == original_len:
            return False

        self._subscriptions[tenant_id] = updated
        logger.debug(
            f"[EventBus] Unsubscribed {handler_id} from tenant '{tenant_id}'"
        )
        return True

    async def publish(self, event: Event) -> int:
        """
        Publish an event to matching subscribers.

        Flow: tenant namespace -> type match (wildcard/exact) -> filter match -> dispatch.
        Returns the number of handlers triggered.
        """
        tenant_subs = [
            *self._subscriptions.get(event.tenant_id, []),
            *self._subscriptions.get("*", []),
        ]
        triggered = 0

        for sub in tenant_subs:
            if not _event_type_matches(sub.event_type, event.type):
                continue
            if not _filter_matches(sub.filter, event.payload):
                continue

            triggered += 1
            try:
                await sub.handler(event)
            except Exception as exc:
                logger.error(
                    f"[EventBus] Handler {sub.handler_id} failed "
                    f"for event {event.event_id}: {exc}"
                )

        return triggered

    def clear_all(self) -> None:
        """Remove all subscriptions (used during cleanup)."""
        self._subscriptions.clear()
        logger.debug("[EventBus] All subscriptions cleared")

    @property
    def subscription_count(self) -> int:
        """Total number of active subscriptions across all tenants."""
        return sum(len(subs) for subs in self._subscriptions.values())
