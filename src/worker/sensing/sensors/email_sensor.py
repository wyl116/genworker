"""Email polling sensor."""
from __future__ import annotations

import json
from typing import Any

from src.channels.dedup import MessageDeduplicator

from ..base import SensorBase
from ..config import RoutingRule
from ..protocol import SensedFact

DEFAULT_EMAIL_ROUTING_RULES: tuple[RoutingRule, ...] = (
    RoutingRule(
        field="subject",
        pattern=r"URGENT|紧急|告警|P0|故障",
        match_mode="regex",
        route="reactive",
    ),
    RoutingRule(
        field="from",
        pattern=r"boss@|ceo@|cto@|vp@",
        match_mode="regex",
        route="both",
    ),
    RoutingRule(
        field="subject",
        pattern=r"审批|approve|confirm",
        match_mode="regex",
        route="reactive",
    ),
)


class EmailSensor(SensorBase):
    """Poll for new emails via email client or tool executor."""

    def __init__(
        self,
        *,
        email_client: Any | None = None,
        tool_executor: Any | None = None,
        deduplicator: MessageDeduplicator | None = None,
        filter_config: dict[str, str] | None = None,
        routing_rules: tuple[RoutingRule, ...] = DEFAULT_EMAIL_ROUTING_RULES,
        fallback_route: str = "heartbeat",
    ) -> None:
        super().__init__(routing_rules=routing_rules, fallback_route=fallback_route)
        self._email_client = email_client
        self._tool_executor = tool_executor
        self._deduplicator = deduplicator
        self._filter = filter_config or {}
        self._seen_ids: set[str] = set()

    @property
    def sensor_type(self) -> str:
        return "email"

    @property
    def delivery_mode(self) -> str:
        return "poll"

    async def poll(self) -> tuple[SensedFact, ...]:
        emails = await self._fetch_emails()
        matched = _filter_emails(emails, self._filter)
        facts: list[SensedFact] = []
        for email in matched:
            msg_id = str(
                email.get("message_id")
                or email.get("source_uri")
                or email.get("subject", "")
            ).strip()
            if not msg_id or msg_id in self._seen_ids:
                continue
            if self._deduplicator is not None:
                if await self._deduplicator.is_duplicate("email", msg_id):
                    self._seen_ids.add(msg_id)
                    continue
            self._seen_ids.add(msg_id)

            payload = (
                ("subject", email.get("subject", "")),
                ("from", email.get("from", "")),
                ("content", email.get("content", "")),
                ("message_id", msg_id),
            )
            route = self._classify_route(payload)
            facts.append(
                SensedFact(
                    source_type="email",
                    event_type="external.email_received",
                    dedupe_key=f"email:{msg_id}",
                    payload=payload,
                    priority_hint=self._priority_for_route(route),
                    cognition_route=route,
                )
            )
        return tuple(facts)

    async def _fetch_emails(self) -> list[dict[str, Any]]:
        search_query = self._filter.get("subject_keywords", "")
        if self._email_client is not None:
            try:
                return await self._email_client.search(search_query)
            except Exception:
                return []
        if self._tool_executor is None:
            return []
        try:
            result = await self._tool_executor.execute("email_search", {"query": search_query})
        except Exception:
            return []
        return _parse_email_results(result)

    def _priority_for_route(self, route: str) -> int:
        if route == "reactive":
            return 40
        if route == "both":
            return 35
        return 20

    def get_snapshot(self) -> dict[str, Any]:
        return {"seen_ids": sorted(self._seen_ids)}

    def restore_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._seen_ids = set(snapshot.get("seen_ids", []))


def _parse_email_results(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and "emails" in result:
        return list(result["emails"])
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return []
        return _parse_email_results(parsed)
    content = getattr(result, "content", None)
    if isinstance(content, str):
        return _parse_email_results(content)
    if isinstance(content, list):
        return content
    return []


def _filter_emails(
    emails: list[dict[str, Any]],
    filter_dict: dict[str, str],
) -> list[dict[str, Any]]:
    keywords = tuple(
        item.strip() for item in str(filter_dict.get("subject_keywords", "")).split(",")
        if item.strip()
    )
    from_domains = tuple(
        item.strip() for item in str(filter_dict.get("from_domains", "")).split(",")
        if item.strip()
    )

    result: list[dict[str, Any]] = []
    for email in emails:
        if keywords:
            subject = str(email.get("subject", ""))
            if not any(keyword in subject for keyword in keywords):
                continue
        if from_domains:
            sender = str(email.get("from", ""))
            if not any(domain in sender for domain in from_domains):
                continue
        result.append(email)
    return result
