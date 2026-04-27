"""
TriggerManager - multi-source trigger management for duties.

Manages:
- schedule triggers via APScheduler cron jobs
- event triggers via EventBus subscriptions
- condition triggers via APScheduler interval jobs
- collaboration and manual triggers (registered but not auto-fired)
"""
from __future__ import annotations

import inspect
import logging
import re
from typing import Any, Awaitable, Callable

from src.events.bus import EventBus, Subscription
from src.events.models import Event

from .models import Duty, DutyTrigger, EventContext

logger = logging.getLogger(__name__)


def select_execution_depth(duty: Duty, trigger_id: str) -> str:
    """
    Pure function: determine execution depth for a trigger.

    Checks overrides first, falls back to default.
    """
    return duty.depth_for_trigger(trigger_id)


def _parse_interval_to_seconds(interval: str) -> int:
    """
    Parse a human-readable interval string to seconds.

    Supports: "5m", "10m", "1h", "30s", "2h30m".
    """
    total = 0
    pattern = re.compile(r"(\d+)([smh])")
    matches = pattern.findall(interval)

    if not matches:
        raise ValueError(f"Invalid interval format: '{interval}'")

    for value_str, unit in matches:
        value = int(value_str)
        if unit == "s":
            total += value
        elif unit == "m":
            total += value * 60
        elif unit == "h":
            total += value * 3600

    return total


def _evaluate_rule(metric_value: float, rule: str) -> bool:
    """
    Evaluate a numeric rule against a metric value.

    Supports:
    - "> 0.1", ">= 5", "<= 10"
    - "between 0.1 and 0.3"
    - percentages like "> 10%"
    """
    clean_rule = (rule or "").strip().lower()
    if not clean_rule:
        raise ValueError("Empty rule")

    between_match = re.fullmatch(
        r"between\s+(-?\d+(?:\.\d+)?)%?\s+and\s+(-?\d+(?:\.\d+)?)%?",
        clean_rule,
    )
    if between_match:
        lower = float(between_match.group(1))
        upper = float(between_match.group(2))
        if "%" in clean_rule and metric_value <= 1.0:
            lower /= 100.0
            upper /= 100.0
        return lower <= metric_value <= upper

    match = re.fullmatch(
        r"(>=|<=|>|<|==|!=)\s*(-?\d+(?:\.\d+)?)(%)?",
        clean_rule,
    )
    if not match:
        raise ValueError(f"Unsupported rule format: '{rule}'")

    operator, threshold_str, is_percent = match.groups()
    threshold = float(threshold_str)
    if is_percent and metric_value <= 1.0:
        threshold /= 100.0

    if operator == ">":
        return metric_value > threshold
    if operator == ">=":
        return metric_value >= threshold
    if operator == "<":
        return metric_value < threshold
    if operator == "<=":
        return metric_value <= threshold
    if operator == "==":
        return metric_value == threshold
    return metric_value != threshold


class TriggerManager:
    """
    Manages multi-source trigger registration for duties.

    Coordinates APScheduler jobs and EventBus subscriptions.
    """

    def __init__(
        self,
        scheduler,  # AsyncIOScheduler (kept generic to avoid hard import)
        event_bus: EventBus,
        duty_executor: "DutyExecutor",  # Forward reference to avoid circular import
        metric_provider: Callable[[Duty, DutyTrigger], Awaitable[float | None] | float | None] | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._event_bus = event_bus
        self._duty_executor = duty_executor
        self._metric_provider = metric_provider
        # duty_id -> list of (type, resource_id) for cleanup
        self._registrations: dict[str, list[tuple[str, str]]] = {}
        self._event_subscription_tenants: dict[str, str] = {}

    async def register_duty(
        self,
        duty: Duty,
        tenant_id: str,
        worker_id: str,
    ) -> None:
        """
        Register all triggers for a duty.

        - schedule -> APScheduler cron job
        - event -> EventBus subscription
        - condition -> APScheduler interval job
        - collaboration/manual -> tracked but not auto-fired
        """
        registrations: list[tuple[str, str]] = []

        for trigger in duty.triggers:
            resource_id = await self._register_trigger(
                duty, trigger, tenant_id, worker_id
            )
            if resource_id:
                registrations.append((trigger.type, resource_id))

        self._registrations[duty.duty_id] = registrations
        logger.info(
            f"[TriggerManager] Registered {len(registrations)} triggers "
            f"for duty '{duty.duty_id}'"
        )

    async def _register_trigger(
        self,
        duty: Duty,
        trigger: DutyTrigger,
        tenant_id: str,
        worker_id: str,
    ) -> str | None:
        """Register a single trigger, returning a resource ID for cleanup."""
        if trigger.type == "schedule":
            return self._register_schedule(duty, trigger, tenant_id, worker_id)
        elif trigger.type == "event":
            return self._register_event(duty, trigger, tenant_id, worker_id)
        elif trigger.type == "condition":
            return self._register_condition(duty, trigger, tenant_id, worker_id)
        elif trigger.type in ("collaboration", "manual"):
            logger.debug(
                f"[TriggerManager] Trigger '{trigger.id}' ({trigger.type}) "
                f"registered (no auto-fire)"
            )
            return f"{trigger.type}:{trigger.id}"
        return None

    def _register_schedule(
        self,
        duty: Duty,
        trigger: DutyTrigger,
        tenant_id: str,
        worker_id: str,
    ) -> str:
        """Register a cron-based schedule trigger."""
        from apscheduler.triggers.cron import CronTrigger

        job_id = f"duty:{duty.duty_id}:trigger:{trigger.id}"
        cron_trigger = CronTrigger.from_crontab(trigger.cron)

        self._scheduler.add_job(
            self._fire_duty,
            trigger=cron_trigger,
            id=job_id,
            args=(duty, trigger, tenant_id, worker_id),
            replace_existing=True,
        )
        logger.debug(f"[TriggerManager] Schedule job '{job_id}' registered")
        return job_id

    def _register_event(
        self,
        duty: Duty,
        trigger: DutyTrigger,
        tenant_id: str,
        worker_id: str,
    ) -> str:
        """Register an EventBus subscription for event triggers."""
        handler_id = f"duty:{duty.duty_id}:trigger:{trigger.id}"

        async def _handler(event: Event) -> None:
            await self._fire_duty(
                duty,
                trigger,
                tenant_id,
                worker_id,
                event_context=EventContext(
                    event_id=event.event_id,
                    event_type=event.type,
                    payload=event.payload,
                    source=event.source,
                ),
            )

        subscription = Subscription(
            handler_id=handler_id,
            event_type=trigger.source or "*",
            tenant_id=tenant_id,
            handler=_handler,
            filter=trigger.filter,
        )
        self._event_bus.subscribe(subscription)
        self._event_subscription_tenants[handler_id] = tenant_id
        logger.debug(
            f"[TriggerManager] Event subscription '{handler_id}' registered"
        )
        return handler_id

    def _register_condition(
        self,
        duty: Duty,
        trigger: DutyTrigger,
        tenant_id: str,
        worker_id: str,
    ) -> str:
        """Register an interval-based condition check."""
        from apscheduler.triggers.interval import IntervalTrigger

        job_id = f"duty:{duty.duty_id}:condition:{trigger.id}"
        seconds = _parse_interval_to_seconds(trigger.check_interval)

        self._scheduler.add_job(
            self._check_condition,
            trigger=IntervalTrigger(seconds=seconds),
            id=job_id,
            args=(duty, trigger, tenant_id, worker_id),
            replace_existing=True,
        )
        logger.debug(
            f"[TriggerManager] Condition job '{job_id}' registered "
            f"(interval: {seconds}s)"
        )
        return job_id

    async def _fire_duty(
        self,
        duty: Duty,
        trigger: DutyTrigger,
        tenant_id: str,
        worker_id: str,
        event_context: EventContext | None = None,
    ) -> None:
        """Execute a duty via the DutyExecutor."""
        try:
            await self._duty_executor.execute(
                duty,
                trigger,
                tenant_id,
                worker_id,
                event_context=event_context,
            )
        except Exception as exc:
            logger.error(
                f"[TriggerManager] Failed to execute duty '{duty.duty_id}' "
                f"trigger '{trigger.id}': {exc}"
            )

    async def _check_condition(
        self,
        duty: Duty,
        trigger: DutyTrigger,
        tenant_id: str,
        worker_id: str,
    ) -> None:
        """
        Check a condition trigger.

        Evaluates the configured metric/rule before firing the duty.
        """
        metric_value = await self._resolve_metric_value(duty, trigger)
        if metric_value is None:
            logger.warning(
                "[TriggerManager] Condition trigger '%s' for duty '%s' "
                "has no metric value for '%s'",
                trigger.id,
                duty.duty_id,
                trigger.metric,
            )
            return
        try:
            should_fire = _evaluate_rule(metric_value, trigger.rule or "")
        except ValueError as exc:
            logger.warning(
                "[TriggerManager] Invalid condition rule for duty '%s' "
                "trigger '%s': %s",
                duty.duty_id,
                trigger.id,
                exc,
            )
            return
        if not should_fire:
            logger.debug(
                "[TriggerManager] Condition skipped for duty '%s' "
                "trigger '%s' (metric=%s, rule=%s)",
                duty.duty_id,
                trigger.id,
                round(metric_value, 4),
                trigger.rule,
            )
            return
        await self._fire_duty(duty, trigger, tenant_id, worker_id)

    async def _resolve_metric_value(
        self,
        duty: Duty,
        trigger: DutyTrigger,
    ) -> float | None:
        """Resolve a numeric metric for a condition trigger."""
        if self._metric_provider is not None:
            result = self._metric_provider(duty, trigger)
            if inspect.isawaitable(result):
                result = await result
            return float(result) if result is not None else None
        return self._load_metric_from_history(duty, trigger)

    def _load_metric_from_history(
        self,
        duty: Duty,
        trigger: DutyTrigger,
    ) -> float | None:
        """Load condition metrics from recent duty execution history."""
        from .execution_log import load_recent_records

        execution_root = getattr(self._duty_executor, "_execution_log_dir", None)
        if execution_root is None:
            return None

        duty_dir = execution_root / duty.duty_id
        records = load_recent_records(duty_dir, limit=20)
        if not records:
            return None

        metric = (trigger.metric or "").strip().lower()
        failure_records = tuple(
            record
            for record in records
            if str(record.conclusion).strip().lower().startswith("error")
        )
        anomaly_total = sum(len(record.anomalies_found) for record in records)

        if metric in {"error_rate", "failure_rate"}:
            return len(failure_records) / len(records)
        if metric == "failure_count":
            return float(len(failure_records))
        if metric == "anomaly_count":
            return float(anomaly_total)
        if metric == "avg_anomaly_count":
            return anomaly_total / len(records)
        if metric == "escalation_rate":
            return sum(1 for record in records if record.escalated) / len(records)
        if metric in {"avg_duration", "avg_duration_seconds"}:
            return sum(record.duration_seconds for record in records) / len(records)
        if metric in {"last_duration", "last_duration_seconds"}:
            return records[-1].duration_seconds

        logger.debug(
            "[TriggerManager] Unsupported condition metric '%s' "
            "for duty '%s'",
            trigger.metric,
            duty.duty_id,
        )
        return None

    async def unregister_duty(self, duty_id: str) -> None:
        """Unregister all triggers for a duty."""
        registrations = self._registrations.pop(duty_id, [])

        for reg_type, resource_id in registrations:
            if reg_type in ("schedule", "condition"):
                try:
                    self._scheduler.remove_job(resource_id)
                except Exception:
                    pass
            elif reg_type == "event":
                tenant_id = self._event_subscription_tenants.pop(resource_id, "")
                if tenant_id:
                    try:
                        self._event_bus.unsubscribe(tenant_id, resource_id)
                    except Exception:
                        pass

        logger.info(
            f"[TriggerManager] Unregistered {len(registrations)} triggers "
            f"for duty '{duty_id}'"
        )

    @property
    def registered_duties(self) -> tuple[str, ...]:
        """List of currently registered duty IDs."""
        return tuple(self._registrations.keys())

    @property
    def registration_snapshot(self) -> dict[str, Any]:
        """Structured registration summary for operational introspection."""
        by_duty: dict[str, dict[str, Any]] = {}
        total_resources = 0
        for duty_id, registrations in self._registrations.items():
            counts: dict[str, int] = {}
            resources: list[dict[str, str]] = []
            for reg_type, resource_id in registrations:
                counts[reg_type] = counts.get(reg_type, 0) + 1
                resources.append({
                    "type": reg_type,
                    "resource_id": resource_id,
                })
                total_resources += 1
            by_duty[duty_id] = {
                "resource_count": len(registrations),
                "counts": counts,
                "resources": resources,
            }
        return {
            "duty_count": len(self._registrations),
            "resource_count": total_resources,
            "duties": by_duty,
        }
