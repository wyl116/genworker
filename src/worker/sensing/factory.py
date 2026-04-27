"""Factory helpers for concrete sensor creation."""
from __future__ import annotations

from typing import Any

from .config import SensorConfig
from .protocol import Sensor


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in str(value or "").split(",") if item.strip())


def create_sensor(config: SensorConfig, **deps: Any) -> Sensor:
    """Create a supported sensor from config and optional dependencies."""
    source = config.source_type
    filter_dict = dict(config.filter)
    tenant_id = str(deps.get("tenant_id", "")).strip()
    worker_id = str(deps.get("worker_id", "")).strip()
    client_factory = deps.get("platform_client_factory")

    if source == "email":
        from .sensors.email_sensor import DEFAULT_EMAIL_ROUTING_RULES, EmailSensor
        email_client = _require_worker_client(
            client_factory,
            tenant_id=tenant_id,
            worker_id=worker_id,
            channel_type="email",
        )

        return EmailSensor(
            email_client=email_client,
            tool_executor=None,
            deduplicator=deps.get("message_deduplicator"),
            filter_config=filter_dict,
            routing_rules=config.routing_rules or DEFAULT_EMAIL_ROUTING_RULES,
            fallback_route=config.fallback_route or "heartbeat",
        )

    if source == "feishu_folder":
        from .sensors.feishu_file_sensor import FeishuFileSensor
        feishu_client = _require_worker_client(
            client_factory,
            tenant_id=tenant_id,
            worker_id=worker_id,
            channel_type="feishu",
        )

        return FeishuFileSensor(
            feishu_client=feishu_client,
            mount_manager=None,
            filter_config=filter_dict,
        )

    if source == "workspace_file":
        from .sensors.workspace_file_sensor import WorkspaceFileSensor

        return WorkspaceFileSensor(
            watch_paths=_split_csv(filter_dict.get("watch_paths", "")),
            patterns=_split_csv(filter_dict.get("patterns", "*")) or ("*",),
        )

    if source == "git":
        from .sensors.git_sensor import DEFAULT_GIT_ROUTING_RULES, GitSensor

        return GitSensor(
            repo_path=filter_dict.get("repo_path", "."),
            branches=_split_csv(filter_dict.get("branches", "main")) or ("main",),
            routing_rules=config.routing_rules or DEFAULT_GIT_ROUTING_RULES,
            fallback_route=config.fallback_route or "heartbeat",
        )

    if source == "webhook":
        from .sensors.webhook_sensor import WebhookSensor

        return WebhookSensor(
            routing_rules=config.routing_rules,
            fallback_route=config.fallback_route or "reactive",
        )

    raise ValueError(f"Unknown sensor source_type: '{source}'")


def _require_worker_client(
    client_factory: Any,
    *,
    tenant_id: str,
    worker_id: str,
    channel_type: str,
) -> Any:
    if client_factory is None:
        raise ValueError(f"platform_client_factory not configured for {channel_type}")
    client = client_factory.get_client(tenant_id, worker_id, channel_type)
    if client is None:
        raise ValueError(
            f"missing {channel_type} credentials for worker {tenant_id}/{worker_id}"
        )
    return client
