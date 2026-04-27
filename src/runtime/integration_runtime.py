"""Integration runtime helpers extracted from bootstrap initialization."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExternalGoalPolicy:
    """Resolved goal creation policy for one worker/source pair."""

    auto_create_goal: bool = True
    require_approval: bool = True


def register_goal_sync_subscriptions(
    *,
    event_bus,
    tenant_id: str,
    sync_manager,
    workspace_root: Path | None = None,
) -> list[tuple[str, str]]:
    """Subscribe to goal escalation events that require stakeholder follow-up."""
    if event_bus is None:
        return []

    from src.events.bus import Subscription
    from src.worker.integrations.goal_generator import find_goal_file
    from src.worker.goal.parser import parse_goal

    handler_id = f"integration-goal-sync-{tenant_id}"

    async def _handle_progress_request(event) -> None:
        payload = dict(getattr(event, "payload", ()))
        goal_file_raw = str(payload.get("goal_file", "")).strip()
        goal_id = str(payload.get("goal_id", "")).strip()
        worker_id = str(payload.get("worker_id", "")).strip()
        goal_file = Path(goal_file_raw) if goal_file_raw else None
        if not worker_id and goal_file is not None:
            worker_id = infer_worker_id_from_goal_file(goal_file)
        if (
            goal_id
            and worker_id
            and (
                goal_file is None
                or not goal_file.is_file()
            )
            and workspace_root is not None
        ):
            resolved = find_goal_file(
                workspace_root / "tenants" / event.tenant_id / "workers" / worker_id / "goals",
                goal_id,
            )
            if resolved is not None:
                goal_file = resolved
        if goal_file is None or not goal_file.is_file():
            logger.warning(
                "[IntegrationRuntime] Goal file missing for progress request: %s",
                goal_file_raw or goal_id,
            )
            return

        try:
            goal = parse_goal(goal_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "[IntegrationRuntime] Failed to parse goal for progress request '%s': %s",
                goal_file.name,
                exc,
            )
            return

        if not worker_id:
            logger.warning(
                "[IntegrationRuntime] Progress request missing worker scope for %s",
                goal_file,
            )
            return

        await sync_manager.request_progress_update(
            goal,
            tenant_id=event.tenant_id,
            worker_id=worker_id,
        )

    event_bus.subscribe(Subscription(
        handler_id=handler_id,
        event_type="goal.progress_update_requested",
        tenant_id=tenant_id,
        handler=_handle_progress_request,
    ))
    return [(tenant_id, handler_id)]


def register_external_content_subscriptions(
    *,
    context,
    event_bus,
    tenant_id: str,
) -> list[tuple[str, str]]:
    """Bridge sensed external content into ContentParser -> Goal generation."""
    if event_bus is None:
        return []

    from src.events.bus import Subscription

    subscriptions: list[tuple[str, str]] = []
    email_bridge_handler_id = f"integration-content-bridge-email-{tenant_id}"
    feishu_bridge_handler_id = f"integration-content-bridge-feishu-{tenant_id}"
    webhook_bridge_handler_id = f"integration-content-bridge-webhook-{tenant_id}"
    parser_handler_id = f"integration-content-parser-{tenant_id}"

    async def _bridge_email_content(event) -> None:
        payload = dict(getattr(event, "payload", ()))
        content_raw = str(payload.get("content", "")).strip()
        worker_id = str(payload.get("worker_id", "")).strip()
        message_id = str(payload.get("message_id", "")).strip()
        if not content_raw or not worker_id:
            return

        policy = resolve_external_goal_policy(
            context=context,
            worker_id=worker_id,
            source_type="email",
        )
        if not policy.auto_create_goal:
            logger.info(
                "[IntegrationRuntime] auto_create_goal disabled for worker '%s' source '%s'",
                worker_id,
                "email",
            )
            return
        source_uri = f"email://{message_id}" if message_id else ""
        await publish_external_content_event(
            event_bus=event_bus,
            tenant_id=event.tenant_id,
            source=event.source,
            worker_id=worker_id,
            source_type="email",
            source_uri=source_uri,
            content=content_raw,
            policy=policy,
        )

    async def _bridge_feishu_content(event) -> None:
        payload = dict(getattr(event, "payload", ()))
        worker_id = str(payload.get("worker_id", "")).strip()
        path = str(payload.get("path", "")).strip()
        if not worker_id or not path:
            return

        policy = resolve_external_goal_policy(
            context=context,
            worker_id=worker_id,
            source_type="feishu_folder",
        )
        if not policy.auto_create_goal:
            logger.info(
                "[IntegrationRuntime] auto_create_goal disabled for worker '%s' source '%s'",
                worker_id,
                "feishu_folder",
            )
            return

        content_raw = await load_feishu_document_content(context=context, path=path)
        if not content_raw:
            logger.debug(
                "[IntegrationRuntime] Feishu content empty or unreadable for path '%s'",
                path,
            )
            return

        await publish_external_content_event(
            event_bus=event_bus,
            tenant_id=event.tenant_id,
            source=event.source,
            worker_id=worker_id,
            source_type="feishu_doc",
            source_uri=path,
            content=content_raw,
            policy=policy,
        )

    async def _bridge_webhook_content(event) -> None:
        if str(getattr(event, "source", "")).strip() != "sensor:webhook":
            return
        if str(getattr(event, "type", "")).strip() == "content.external_received":
            return

        payload = dict(getattr(event, "payload", ()))
        worker_id = str(payload.get("worker_id", "")).strip()
        if not worker_id:
            return

        policy = resolve_external_goal_policy(
            context=context,
            worker_id=worker_id,
            source_type="webhook",
        )
        if not policy.auto_create_goal:
            logger.info(
                "[IntegrationRuntime] auto_create_goal disabled for worker '%s' source '%s'",
                worker_id,
                "webhook",
            )
            return

        content_raw = extract_webhook_content(payload)
        if not content_raw:
            logger.debug(
                "[IntegrationRuntime] Webhook payload had no usable content for worker '%s'",
                worker_id,
            )
            return

        source_uri = str(payload.get("source_uri", "")).strip()
        if not source_uri:
            source_uri = f"webhook://{event.type}/{event.event_id}"

        await publish_external_content_event(
            event_bus=event_bus,
            tenant_id=event.tenant_id,
            source=event.source,
            worker_id=worker_id,
            source_type="webhook",
            source_uri=source_uri,
            content=content_raw,
            policy=policy,
        )

    async def _handle_external_content(event) -> None:
        from src.common.content_scanner import scan

        payload = dict(getattr(event, "payload", ()))
        worker_id = str(payload.get("worker_id", "")).strip()
        content_raw = str(payload.get("content", "")).strip()
        source_type = str(payload.get("source_type", "")).strip()
        source_uri = str(payload.get("source_uri", "")).strip()
        if not worker_id or not content_raw:
            return

        policy = resolve_external_goal_policy(
            context=context,
            worker_id=worker_id,
            source_type=source_type or "external",
        )
        auto_create_goal = coerce_bool(
            payload.get("auto_create_goal"),
            default=policy.auto_create_goal,
        )
        require_approval = coerce_bool(
            payload.get("require_approval"),
            default=policy.require_approval,
        )
        if not auto_create_goal:
            logger.info(
                "[IntegrationRuntime] auto_create_goal disabled for worker '%s' source '%s'",
                worker_id,
                source_type or "external",
            )
            return
        scan_result = scan(content_raw)
        if not scan_result.is_safe:
            logger.warning(
                "[IntegrationRuntime] Rejected external content for worker '%s': %s",
                worker_id,
                scan_result.reason,
            )
            inbox_store = context.get_state("integration_inbox_store")
            if inbox_store is not None:
                from src.autonomy.inbox import InboxItem

                await inbox_store.write(
                    InboxItem(
                        tenant_id=event.tenant_id,
                        worker_id=worker_id,
                        target_session_key=f"main:{worker_id}",
                        source_type="external_content_scan",
                        event_type="content.external_rejected",
                        priority_hint=10,
                        dedupe_key=f"external_rejected:{worker_id}:{source_uri or source_type}",
                        payload={
                            "source_type": source_type or "external",
                            "source_uri": source_uri,
                            "scan_reason": scan_result.reason,
                            "task_description": (
                                "发现一条未通过安全扫描的外部内容，"
                                "需要人工确认是否值得后续跟进。"
                            ),
                        },
                    )
                )
            return

        content_parser = context.get_state("content_parser")
        if content_parser is None:
            return

        parsed = await content_parser.parse(
            content=content_raw,
            source_type=source_type or "external",
            context={"source_uri": source_uri},
        )
        if parsed is None:
            logger.debug(
                "[IntegrationRuntime] Content parsing returned None, skipping goal generation"
            )
            return

        workspace_root = Path(context.get_state("workspace_root", "workspace"))
        goals_dir = (
            workspace_root / "tenants" / event.tenant_id / "workers" / worker_id / "goals"
        )

        from src.worker.goal.parser import parse_goal
        from src.worker.integrations.goal_generator import (
            find_goal_file_by_source_uri,
            generate_goal_from_parsed,
            update_goal_from_external,
            write_goal_md,
        )

        existing_goal_file = (
            find_goal_file_by_source_uri(goals_dir, source_uri)
            if source_uri
            else None
        )
        if existing_goal_file is not None:
            llm_client = context.get_state("llm_client")
            if llm_client is None:
                logger.info(
                    "[IntegrationRuntime] Existing goal matched source_uri '%s'; skipping duplicate creation because llm_client is unavailable",
                    source_uri,
                )
                return
            try:
                existing_goal = parse_goal(existing_goal_file.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(
                    "[IntegrationRuntime] Failed to parse existing goal '%s' for source update: %s",
                    existing_goal_file,
                    exc,
                )
            else:
                updated_goal = await update_goal_from_external(
                    existing_goal,
                    content_raw,
                    llm_client,
                )
                write_goal_md(updated_goal, goals_dir)
                logger.info(
                    "[IntegrationRuntime] Goal updated from external content: %s",
                    updated_goal.goal_id,
                )
                return

        goal = await generate_goal_from_parsed(
            parsed=parsed,
            goals_dir=goals_dir,
            require_approval=require_approval,
            event_bus=event_bus,
            tenant_id=event.tenant_id,
            worker_id=worker_id,
        )
        logger.info(
            "[IntegrationRuntime] Goal created from external content: %s",
            goal.goal_id,
        )

    event_bus.subscribe(Subscription(
        handler_id=email_bridge_handler_id,
        event_type="external.email_received",
        tenant_id=tenant_id,
        handler=_bridge_email_content,
    ))
    subscriptions.append((tenant_id, email_bridge_handler_id))

    event_bus.subscribe(Subscription(
        handler_id=feishu_bridge_handler_id,
        event_type="external.feishu_doc_updated",
        tenant_id=tenant_id,
        handler=_bridge_feishu_content,
    ))
    subscriptions.append((tenant_id, feishu_bridge_handler_id))

    event_bus.subscribe(Subscription(
        handler_id=webhook_bridge_handler_id,
        event_type="*",
        tenant_id=tenant_id,
        handler=_bridge_webhook_content,
    ))
    subscriptions.append((tenant_id, webhook_bridge_handler_id))

    event_bus.subscribe(Subscription(
        handler_id=parser_handler_id,
        event_type="content.external_received",
        tenant_id=tenant_id,
        handler=_handle_external_content,
    ))
    subscriptions.append((tenant_id, parser_handler_id))

    return subscriptions


def register_goal_approval_subscription(
    *,
    context,
    apscheduler,
    event_bus,
    tenant_id: str,
) -> list[tuple[str, str]]:
    """Register health checks when a pending goal is approved."""
    if event_bus is None or apscheduler is None:
        return []

    from src.events.bus import Subscription
    from src.runtime.scheduler_runtime import register_single_goal_health_check
    from src.worker.integrations.goal_generator import find_goal_file
    from src.worker.goal.parser import parse_goal

    handler_id = f"goal-approved-scheduler-{tenant_id}"

    async def _handle_goal_approved(event) -> None:
        payload = dict(getattr(event, "payload", ()))
        goal_file_str = str(payload.get("goal_file", "")).strip()
        goal_id = str(payload.get("goal_id", "")).strip()
        worker_id = str(payload.get("worker_id", "")).strip()
        event_tenant_id = str(payload.get("tenant_id", event.tenant_id)).strip() or event.tenant_id
        if not worker_id:
            return

        goal_file = Path(goal_file_str) if goal_file_str else None
        if (
            goal_id
            and (
                goal_file is None
                or not goal_file.is_file()
            )
        ):
            workspace_root = Path(context.get_state("workspace_root", "workspace"))
            resolved = find_goal_file(
                workspace_root / "tenants" / event_tenant_id / "workers" / worker_id / "goals",
                goal_id,
            )
            if resolved is not None:
                goal_file = resolved
        if goal_file is None or not goal_file.is_file():
            return

        try:
            goal = parse_goal(goal_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[GoalApproval] Failed to parse goal: %s", exc)
            return

        worker_schedulers = context.get_state("worker_schedulers", {})
        worker_scheduler = worker_schedulers.get(worker_id)
        if worker_scheduler is None:
            logger.warning(
                "[GoalApproval] Worker scheduler missing for worker '%s'",
                worker_id,
            )
            return

        register_single_goal_health_check(
            scheduler=apscheduler,
            goal=goal,
            goal_file=goal_file,
            tenant_id=event_tenant_id,
            worker_id=worker_id,
            worker_scheduler=worker_scheduler,
            event_bus=event_bus,
            inbox_store=context.get_state("integration_inbox_store"),
            workspace_root=Path(context.get_state("workspace_root", "workspace")),
        )
        logger.info(
            "[GoalApproval] Goal '%s' approved, health check registered",
            goal.goal_id,
        )

    event_bus.subscribe(Subscription(
        handler_id=handler_id,
        event_type="goal.approved",
        tenant_id=tenant_id,
        handler=_handle_goal_approved,
    ))
    return [(tenant_id, handler_id)]


def infer_worker_id_from_goal_file(goal_file: Path) -> str:
    parts = goal_file.parts
    if "workers" not in parts:
        return ""
    workers_index = parts.index("workers")
    if workers_index + 1 >= len(parts):
        return ""
    return str(parts[workers_index + 1]).strip()


def resolve_external_goal_policy(
    *,
    context,
    worker_id: str,
    source_type: str,
) -> ExternalGoalPolicy:
    """
    Resolve auto-goal policy from the worker's sensor configuration.

    Falls back to the legacy behavior (auto create + approval required) when
    the worker or sensor config cannot be resolved.
    """
    worker_registry = context.get_state("worker_registry")
    if worker_registry is None or not hasattr(worker_registry, "get"):
        return ExternalGoalPolicy()

    entry = worker_registry.get(worker_id)
    if entry is None:
        return ExternalGoalPolicy()

    from src.worker.sensing.config import parse_sensor_config

    normalized_source = normalize_source_type(source_type)
    for raw in getattr(entry.worker, "sensor_configs", ()) or ():
        config = parse_sensor_config(raw)
        if normalize_source_type(config.source_type) != normalized_source:
            continue
        return ExternalGoalPolicy(
            auto_create_goal=config.auto_create_goal,
            require_approval=config.require_approval,
        )

    return ExternalGoalPolicy()


def normalize_source_type(source_type: str) -> str:
    aliases = {
        "feishu_doc": "feishu_folder",
        "feishu_document": "feishu_folder",
    }
    normalized = str(source_type or "").strip().lower()
    return aliases.get(normalized, normalized)


def coerce_bool(value, *, default: bool) -> bool:
    """Parse common truthy/falsey forms while preserving a default."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n"}:
            return False
    return bool(value)


async def publish_external_content_event(
    *,
    event_bus,
    tenant_id: str,
    source: str,
    worker_id: str,
    source_type: str,
    source_uri: str,
    content: str,
    policy: ExternalGoalPolicy,
) -> None:
    """Publish normalized external content for downstream parsing."""
    from src.events.models import Event

    await event_bus.publish(Event(
        event_id=f"evt-{uuid4().hex[:8]}",
        type="content.external_received",
        source=source,
        tenant_id=tenant_id,
        payload=(
            ("content", content),
            ("source_type", source_type),
            ("source_uri", source_uri),
            ("worker_id", worker_id),
            ("auto_create_goal", policy.auto_create_goal),
            ("require_approval", policy.require_approval),
        ),
    ))


async def load_feishu_document_content(*, context, path: str) -> str:
    """Read Feishu document content through the configured mount manager."""
    mount_manager = context.get_state("mount_manager")
    if mount_manager is None or not hasattr(mount_manager, "read_file"):
        return ""

    try:
        content = await mount_manager.read_file(path)
    except Exception as exc:
        logger.warning(
            "[IntegrationRuntime] Failed to read Feishu content from '%s': %s",
            path,
            exc,
        )
        return ""

    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace").strip()
    return str(content).strip()


def extract_webhook_content(payload: dict) -> str:
    """Normalize webhook payload into a text blob for ContentParser."""
    for field in ("content", "body", "text", "message", "description"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()

    filtered = {
        str(key): value
        for key, value in payload.items()
        if key not in {"worker_id", "auto_create_goal", "require_approval"}
    }
    if not filtered:
        return ""

    return json.dumps(filtered, ensure_ascii=False, sort_keys=True)
