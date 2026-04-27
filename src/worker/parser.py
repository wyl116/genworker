"""
PERSONA.md parser - extracts YAML frontmatter into a frozen Worker object.

Parses files with the format:
  ---
  (YAML frontmatter with identity, tool_policy, constraints, etc.)
  ---
  (Markdown body injected into system prompt)
"""
import re
from pathlib import Path

import yaml

from src.common.exceptions import WorkerException
from src.common.logger import get_logger

from .models import (
    ServiceConfig,
    Worker,
    WorkerHeartbeatConfig,
    WorkerIdentity,
    WorkerMode,
    WorkerPersonality,
    WorkerToolPolicy,
)
from .contacts.models import ContactRegistryConfig, PersonIdentity, PersonProfile

logger = get_logger()

_FRONTMATTER_PATTERN = re.compile(
    r"\A\s*---\s*\n(.*?)\n---\s*\n?(.*)",
    re.DOTALL,
)


def parse_persona_md(path: Path) -> Worker:
    """
    Parse a PERSONA.md file into a frozen Worker object.

    Args:
        path: Path to the PERSONA.md file.

    Returns:
        Frozen Worker instance.

    Raises:
        WorkerException: If the file cannot be read or parsed.
    """
    text = _read_file(path)
    frontmatter_raw, body = _split_frontmatter(text, path)
    frontmatter = _parse_yaml(frontmatter_raw, path)
    return _build_worker(frontmatter, body, str(path))


def _read_file(path: Path) -> str:
    """Read file content with error handling."""
    if not path.is_file():
        raise WorkerException(f"PERSONA.md not found: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkerException(f"Cannot read PERSONA.md {path}: {exc}") from exc


def _split_frontmatter(text: str, path: Path) -> tuple[str, str]:
    """Split YAML frontmatter from markdown body."""
    match = _FRONTMATTER_PATTERN.match(text)
    if not match:
        raise WorkerException(
            f"Invalid PERSONA.md format in {path}: "
            f"missing YAML frontmatter (expected --- delimiters)"
        )
    return match.group(1), match.group(2)


def _parse_yaml(raw: str, path: Path) -> dict:
    """Parse YAML frontmatter string."""
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise WorkerException(
            f"Invalid YAML in PERSONA.md {path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise WorkerException(
            f"YAML frontmatter in {path} is not a mapping"
        )
    return data


def _build_worker(fm: dict, body: str, source_path: str) -> Worker:
    """Build a frozen Worker from parsed frontmatter and body."""
    identity = _parse_identity(fm.get("identity", {}), source_path)
    mode = _parse_mode(fm.get("mode", WorkerMode.PERSONAL.value), source_path)

    if not identity.worker_id:
        raise WorkerException(
            f"Missing required 'identity.worker_id' in {source_path}"
        )

    tool_policy = _parse_tool_policy(fm.get("tool_policy", {}))
    constraints = tuple(str(c) for c in fm.get("constraints", []))
    triggers = tuple(
        dict(t) if isinstance(t, dict) else {"action": str(t)}
        for t in fm.get("triggers", [])
    )
    sensor_config_entries = fm.get("sensor_configs", fm.get("monitor_configs", []))
    sensor_configs = tuple(
        dict(m) if isinstance(m, dict) else {"source_type": str(m)}
        for m in sensor_config_entries
    )
    channels = tuple(
        dict(channel) if isinstance(channel, dict) else {"type": str(channel)}
        for channel in fm.get("channels", [])
    )
    configured_contacts = _parse_contacts(
        fm.get("contacts", fm.get("configured_contacts", []))
    )
    contacts_config = _parse_contact_settings(
        fm.get("contact_settings", fm.get("contacts_config", {}))
    )
    service_config = _parse_service_config(
        fm.get("service", {}),
        mode=mode,
    )
    heartbeat_config = _parse_heartbeat_config(fm.get("heartbeat", {}))

    return Worker(
        identity=identity,
        mode=mode,
        service_config=service_config,
        heartbeat_config=heartbeat_config,
        tool_policy=tool_policy,
        skills_dir=str(fm.get("skills_dir", "skills/")),
        default_skill=str(fm.get("default_skill", "")),
        constraints=constraints,
        triggers=triggers,
        sensor_configs=sensor_configs,
        channels=channels,
        configured_contacts=configured_contacts,
        contacts_config=contacts_config,
        body_instructions=body.strip(),
        source_path=source_path,
    )


def _parse_mode(raw: object, source_path: str) -> WorkerMode:
    """Parse top-level worker mode."""
    value = str(raw or WorkerMode.PERSONAL.value).strip().lower()
    try:
        return WorkerMode(value)
    except ValueError as exc:
        raise WorkerException(
            f"Invalid PERSONA.md mode '{value}' in {source_path}; "
            f"expected one of: personal, team_member, service"
        ) from exc


def _parse_identity(raw: dict, source_path: str) -> WorkerIdentity:
    """Parse identity section from frontmatter."""
    if not isinstance(raw, dict):
        raise WorkerException(
            f"'identity' must be a mapping in {source_path}"
        )

    personality_raw = raw.get("personality", {})
    personality = _parse_personality(personality_raw)

    principles = tuple(str(p) for p in raw.get("principles", []))

    return WorkerIdentity(
        name=str(raw.get("name", "")),
        worker_id=str(raw.get("worker_id", "")),
        version=str(raw.get("version", "1.0")),
        role=str(raw.get("role", "")),
        department=str(raw.get("department", "")),
        reports_to=str(raw.get("reports_to", "")),
        background=str(raw.get("background", "")).strip(),
        personality=personality,
        principles=principles,
    )


def _parse_personality(raw: dict | None) -> WorkerPersonality:
    """Parse personality section."""
    if not raw or not isinstance(raw, dict):
        return WorkerPersonality()
    return WorkerPersonality(
        traits=tuple(str(t) for t in raw.get("traits", [])),
        communication_style=str(raw.get("communication_style", "")),
        decision_style=str(raw.get("decision_style", "")),
    )


def _parse_tool_policy(raw: dict | None) -> WorkerToolPolicy:
    """Parse tool_policy section using frozenset."""
    if not raw or not isinstance(raw, dict):
        return WorkerToolPolicy()

    mode = str(raw.get("mode", "blacklist")).lower()
    if mode not in ("blacklist", "whitelist"):
        logger.warning(
            f"Unknown tool_policy mode '{mode}', defaulting to 'blacklist'"
        )
        mode = "blacklist"

    denied = frozenset(str(t) for t in raw.get("denied_tools", []))
    allowed = frozenset(str(t) for t in raw.get("allowed_tools", []))

    return WorkerToolPolicy(
        mode=mode,
        denied_tools=denied,
        allowed_tools=allowed,
    )


def _parse_contacts(raw: object) -> tuple[PersonProfile, ...]:
    """Parse configured contacts from PERSONA frontmatter."""
    if not isinstance(raw, list):
        return ()
    contacts: list[PersonProfile] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            continue
        identities_raw = item.get("identities", [])
        identities = tuple(
            PersonIdentity(
                channel_type=str(identity.get("channel_type", "")),
                handle=str(identity.get("handle", "")),
                display_name=str(identity.get("display_name", "")),
                email=str(identity.get("email", "")),
                source=str(identity.get("source", "configured")),
            )
            for identity in identities_raw
            if isinstance(identity, dict)
        )
        contacts.append(PersonProfile(
            person_id=str(item.get("person_id", f"configured-{index}")),
            primary_name=str(item.get("name", item.get("primary_name", ""))),
            role=str(item.get("role", "")),
            organization=str(item.get("organization", "")),
            notes=str(item.get("notes", "")),
            confidence=float(item.get("confidence", 1.0) or 1.0),
            identities=identities,
            source=str(item.get("source", "configured")),
            social_circles=tuple(str(entry) for entry in item.get("social_circles", [])),
            hierarchy_level=str(item.get("hierarchy_level", "")),
            aliases=tuple(str(entry) for entry in item.get("aliases", [])),
            tags=tuple(str(entry) for entry in item.get("tags", [])),
            service_count=int(item.get("service_count", 0) or 0),
            common_topics=tuple(str(entry) for entry in item.get("common_topics", [])),
        ))
    return tuple(contacts)


def _parse_contact_settings(raw: object) -> ContactRegistryConfig:
    """Parse contact registry config overrides."""
    if not isinstance(raw, dict):
        return ContactRegistryConfig()
    return ContactRegistryConfig(
        workspace_root=str(raw.get("workspace_root", "workspace")),
        discovered_dir=str(raw.get("discovered_dir", "discovered")),
        configured_dir=str(raw.get("configured_dir", "configured")),
        index_file=str(raw.get("index_file", "index.jsonl")),
        context_limit=int(raw.get("context_limit", 8) or 8),
    )


def _parse_service_config(
    raw: object,
    *,
    mode: WorkerMode,
) -> ServiceConfig | None:
    """Parse service-mode configuration."""
    if not isinstance(raw, dict):
        return ServiceConfig() if mode == WorkerMode.SERVICE else None

    escalation_raw = raw.get("escalation", {})
    if not isinstance(escalation_raw, dict):
        escalation_raw = {}

    knowledge_sources = tuple(
        dict(entry) if isinstance(entry, dict) else {"value": str(entry)}
        for entry in raw.get("knowledge_sources", [])
    )

    return ServiceConfig(
        knowledge_sources=knowledge_sources,
        session_ttl=int(raw.get("session_ttl", 1800) or 1800),
        max_concurrent_sessions=int(
            raw.get("max_concurrent_sessions", 50) or 50
        ),
        anonymous_allowed=_coerce_bool(
            raw.get("anonymous_allowed", True),
            default=True,
        ),
        escalation_enabled=_coerce_bool(
            escalation_raw.get("enabled", False),
            default=False,
        ),
        escalation_target=str(escalation_raw.get("target_worker", "")),
        escalation_triggers=tuple(
            str(item) for item in escalation_raw.get("triggers", [])
        ),
    )


def _parse_heartbeat_config(raw: object) -> WorkerHeartbeatConfig:
    """Parse worker-level heartbeat strategy overrides."""
    if not isinstance(raw, dict):
        return WorkerHeartbeatConfig()
    return WorkerHeartbeatConfig(
        goal_task_actions=tuple(
            str(item).strip().lower()
            for item in raw.get("goal_task_actions", [])
            if str(item).strip()
        ),
        goal_isolated_actions=tuple(
            str(item).strip().lower()
            for item in raw.get("goal_isolated_actions", [])
            if str(item).strip()
        ),
        goal_isolated_deviation_threshold=(
            float(raw.get("goal_isolated_deviation_threshold"))
            if raw.get("goal_isolated_deviation_threshold") is not None
            else None
        ),
    )


def _coerce_bool(raw: object, *, default: bool) -> bool:
    """Coerce YAML/string values into booleans."""
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return bool(raw)
