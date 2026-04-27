"""
SKILL.md parser - extracts YAML frontmatter and markdown instructions.

Parses files with the format:
  ---
  (YAML frontmatter)
  ---
  (Markdown body with ## instructions.{phase} headers)
"""
import re
from pathlib import Path
from typing import Any, Mapping

import yaml

from src.common.exceptions import SkillException
from src.common.logger import get_logger

from .models import (
    EdgeDefinition,
    FallbackConfig,
    GraphDefinition,
    NodeDefinition,
    NodeKind,
    RetryConfig,
    Skill,
    SkillKeyword,
    SkillScope,
    SkillStrategy,
    StrategyMode,
    WorkflowStep,
    WorkflowStepType,
)

logger = get_logger()

_PYTHON_MODULE_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")

_FRONTMATTER_PATTERN = re.compile(
    r"\A\s*---\s*\n(.*?)\n---\s*\n(.*)",
    re.DOTALL,
)

_INSTRUCTION_HEADER_PATTERN = re.compile(
    r"^##\s+instructions\.(\S+)\s*$",
    re.MULTILINE,
)


def parse_skill_file(path: Path) -> Skill:
    """
    Parse a SKILL.md file into a Skill object.

    Args:
        path: Path to the SKILL.md file.

    Returns:
        Parsed Skill instance.

    Raises:
        SkillException: If the file cannot be parsed.
    """
    text = _read_file(path)
    frontmatter_raw, body = _split_frontmatter(text, path)
    frontmatter = _parse_yaml(frontmatter_raw, path)
    instructions = _parse_instructions(body)
    return _build_skill(frontmatter, instructions, str(path))


def _read_file(path: Path) -> str:
    """Read file content with error handling."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillException(f"Cannot read skill file {path}: {exc}") from exc


def _split_frontmatter(text: str, path: Path) -> tuple[str, str]:
    """Split YAML frontmatter from markdown body."""
    match = _FRONTMATTER_PATTERN.match(text)
    if not match:
        raise SkillException(
            f"Invalid SKILL.md format in {path}: missing YAML frontmatter"
        )
    return match.group(1), match.group(2)


def _parse_yaml(raw: str, path: Path) -> dict:
    """Parse YAML frontmatter string."""
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SkillException(
            f"Invalid YAML in {path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise SkillException(f"YAML frontmatter in {path} is not a mapping")
    return data


def _parse_instructions(body: str) -> Mapping[str, str]:
    """
    Extract phased instructions from markdown body.

    Splits on ## instructions.{phase} headers.
    Content before the first header is stored under "general".
    """
    headers = list(_INSTRUCTION_HEADER_PATTERN.finditer(body))
    if not headers:
        stripped = body.strip()
        if stripped:
            return {"general": stripped}
        return {}

    instructions: dict[str, str] = {}

    # Content before first header goes to "general"
    pre_header = body[: headers[0].start()].strip()
    if pre_header:
        instructions["general"] = pre_header

    for i, header_match in enumerate(headers):
        phase = header_match.group(1)
        start = header_match.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(body)
        content = body[start:end].strip()
        if content:
            instructions[phase] = content

    return instructions


def _build_skill(
    fm: dict,
    instructions: Mapping[str, str],
    source_path: str,
) -> Skill:
    """Build a Skill from parsed frontmatter and instructions."""
    source_format = _detect_source_format(fm)
    metadata = fm.get("metadata", {})
    metadata_map = metadata if isinstance(metadata, dict) else {}
    lw_raw = metadata_map.get("genworker", {})
    lw = lw_raw if isinstance(lw_raw, dict) else {}

    if fm.get("skill_id") and fm.get("name"):
        logger.warning(
            "[SkillParser] Mixed legacy/v2 skill format in %s; "
            "treating as genworker_legacy",
            source_path,
        )

    skill_id = str(fm.get("skill_id") or fm.get("name") or "").strip()
    if not skill_id:
        raise SkillException(
            f"Missing required 'skill_id' or 'name' in {source_path}"
        )

    if source_format == "genworker_legacy":
        resolved_name = str(fm.get("name", skill_id) or skill_id)
    else:
        resolved_name = skill_id

    extra_metadata = {
        key: value for key, value in metadata_map.items()
        if key != "genworker"
    }

    def _ext(key: str, default: Any) -> Any:
        if key in lw:
            return lw.get(key, default)
        if source_format == "genworker_legacy":
            return fm.get(key, default)
        return default

    return Skill(
        skill_id=skill_id,
        name=resolved_name,
        description=str(fm.get("description", "") or ""),
        version=str(fm.get("version", "1.0") or "1.0"),
        scope=_parse_scope(_ext("scope", "system")),
        priority=int(_ext("priority", 0)),
        strategy=_parse_strategy(_ext("strategy", {})),
        keywords=_parse_keywords(_ext("keywords", [])),
        recommended_tools=tuple(
            str(t) for t in _ext("recommended_tools", [])
        ),
        gate_level=str(_ext("gate_level", "gated") or "gated"),
        default_skill=bool(_ext("default_skill", False)),
        instructions=instructions,
        source_format=source_format,
        extra_metadata=extra_metadata,
        source_path=source_path,
    )


def _detect_source_format(fm: dict) -> str:
    """Detect which skill frontmatter format is being used."""
    metadata = fm.get("metadata", {})
    metadata_map = metadata if isinstance(metadata, dict) else {}
    lw = metadata_map.get("genworker", {})

    if fm.get("skill_id"):
        return "genworker_legacy"
    if fm.get("name") and isinstance(lw, dict) and lw:
        return "genworker_v2"
    if fm.get("name"):
        return "openclaw"
    raise SkillException("Missing required 'skill_id' or 'name'")


def _parse_scope(raw: str) -> SkillScope:
    """Parse scope string into SkillScope enum."""
    try:
        return SkillScope(str(raw).lower())
    except ValueError:
        logger.warning(f"Unknown scope '{raw}', defaulting to SYSTEM")
        return SkillScope.SYSTEM


def _parse_strategy(raw: dict | None) -> SkillStrategy:
    """Parse strategy configuration."""
    if not raw:
        return SkillStrategy()

    mode = _parse_strategy_mode(raw.get("mode", "autonomous"))
    workflow = tuple(
        _parse_workflow_step(step_data)
        for step_data in raw.get("workflow", [])
    )
    fallback = _parse_fallback(raw.get("fallback"))
    graph = _parse_graph(raw.get("graph"), mode=mode)

    return SkillStrategy(
        mode=mode,
        workflow=workflow,
        fallback=fallback,
        graph=graph,
    )


def _parse_strategy_mode(raw: str) -> StrategyMode:
    """Parse strategy mode string."""
    try:
        return StrategyMode(str(raw).lower())
    except ValueError:
        logger.warning(
            f"Unknown strategy mode '{raw}', defaulting to AUTONOMOUS"
        )
        return StrategyMode.AUTONOMOUS


def _parse_workflow_step(data: dict) -> WorkflowStep:
    """Parse a single workflow step."""
    retry_data = data.get("retry", {})
    retry = RetryConfig(
        max_attempts=int(retry_data.get("max_attempts", 1)),
        backoff=str(retry_data.get("backoff", "fixed")),
    ) if retry_data else RetryConfig()

    step_type_raw = data.get("type", "autonomous")
    try:
        step_type = WorkflowStepType(str(step_type_raw).lower())
    except ValueError:
        step_type = WorkflowStepType.AUTONOMOUS

    return WorkflowStep(
        step=str(data.get("step", "")),
        type=step_type,
        instruction_ref=str(data.get("instruction_ref", "")),
        max_rounds=int(data.get("max_rounds", 1)),
        tools=tuple(str(t) for t in data.get("tools", [])),
        retry=retry,
    )


def _parse_fallback(data: dict | None) -> FallbackConfig | None:
    """Parse fallback configuration."""
    if not data:
        return None
    return FallbackConfig(
        condition=str(data.get("condition", "")),
        mode=str(data.get("mode", "autonomous")),
    )


def _parse_graph(
    raw: dict | None,
    *,
    mode: StrategyMode,
) -> GraphDefinition | None:
    """Parse langgraph graph definition."""
    if mode != StrategyMode.LANGGRAPH:
        return None
    if not raw:
        raise SkillException("strategy.mode=langgraph requires strategy.graph")
    if not isinstance(raw, dict):
        raise SkillException("strategy.graph must be a mapping")

    yaml_markers = ("nodes", "edges", "entry")
    python_markers = ("module", "factory")
    has_yaml_source = any(key in raw for key in yaml_markers)
    has_python_source = any(key in raw for key in python_markers)
    if has_yaml_source and has_python_source:
        raise SkillException("strategy.graph cannot mix YAML and Python sources")
    if not has_yaml_source and not has_python_source:
        raise SkillException("strategy.graph must define either YAML nodes/edges/entry or Python module/factory")

    max_steps = int(raw.get("max_steps", 50) or 50)
    if has_python_source:
        module = str(raw.get("module", "") or "").strip()
        factory = str(raw.get("factory", "") or "").strip()
        if not module or not factory:
            raise SkillException("Python graph requires both module and factory")
        if _PYTHON_MODULE_PATTERN.match(module) is None:
            raise SkillException(f"Invalid Python graph module path '{module}'")
        return GraphDefinition(
            source="python",
            module=module,
            factory=factory,
            state_schema_ref=str(raw.get("state_schema_ref", "") or "").strip(),
            max_steps=max_steps,
        )

    nodes = tuple(_parse_node_definition(item) for item in raw.get("nodes", ()))
    if not nodes:
        raise SkillException("YAML graph requires at least one node")
    node_names = {node.name for node in nodes}
    entry = str(raw.get("entry", "") or "").strip()
    if not entry:
        raise SkillException("YAML graph requires entry")
    if entry not in node_names:
        raise SkillException(f"Graph entry '{entry}' is not a declared node")
    edges = tuple(_parse_edge_definition(item) for item in raw.get("edges", ()))
    for edge in edges:
        if edge.from_node not in node_names:
            raise SkillException(f"Graph edge references unknown from_node '{edge.from_node}'")
        if edge.to_node != "END" and edge.to_node not in node_names:
            raise SkillException(f"Graph edge references unknown to_node '{edge.to_node}'")
    return GraphDefinition(
        source="yaml",
        state_schema=_parse_state_schema(raw.get("state_schema")),
        entry=entry,
        nodes=nodes,
        edges=edges,
        max_steps=max_steps,
    )


def _parse_state_schema(raw: Any) -> Mapping[str, str]:
    """Parse graph state schema mapping."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SkillException("graph.state_schema must be a mapping")
    return {
        str(key): str(value)
        for key, value in raw.items()
        if str(key).strip()
    }


def _parse_node_definition(data: Any) -> NodeDefinition:
    """Parse one declarative graph node."""
    if not isinstance(data, dict):
        raise SkillException("graph.nodes entries must be mappings")
    name = str(data.get("name", "") or "").strip()
    if not name:
        raise SkillException("graph node missing name")
    try:
        kind = NodeKind(str(data.get("kind", "") or "").strip().lower())
    except ValueError as exc:
        raise SkillException(f"graph node '{name}' has invalid kind") from exc
    route_raw = data.get("route", {})
    if route_raw and not isinstance(route_raw, dict):
        raise SkillException(f"graph node '{name}' route must be a mapping")
    return NodeDefinition(
        name=name,
        kind=kind,
        tool=str(data.get("tool", "") or "").strip(),
        instruction_ref=str(data.get("instruction_ref", "") or "").strip(),
        tools=tuple(str(item).strip() for item in data.get("tools", ()) if str(item).strip()),
        prompt_ref=str(data.get("prompt_ref", "") or "").strip(),
        inbox_event_type=str(data.get("inbox_event_type", "langgraph.interrupt") or "langgraph.interrupt").strip(),
        route={
            str(key): str(value)
            for key, value in (route_raw.items() if isinstance(route_raw, dict) else ())
            if str(key).strip() and str(value).strip()
        },
    )


def _parse_edge_definition(data: Any) -> EdgeDefinition:
    """Parse one declarative graph edge, accepting from/to aliases."""
    if not isinstance(data, dict):
        raise SkillException("graph.edges entries must be mappings")
    from_aliases = ("from", "from_node")
    to_aliases = ("to", "to_node")
    if all(alias not in data for alias in from_aliases):
        raise SkillException("graph edge missing from/from_node")
    if all(alias not in data for alias in to_aliases):
        raise SkillException("graph edge missing to/to_node")
    if "from" in data and "from_node" in data:
        raise SkillException("graph edge cannot define both from and from_node")
    if "to" in data and "to_node" in data:
        raise SkillException("graph edge cannot define both to and to_node")
    return EdgeDefinition(
        from_node=str(data.get("from_node", data.get("from", "")) or "").strip(),
        to_node=str(data.get("to_node", data.get("to", "")) or "").strip(),
        cond=str(data.get("cond", "") or "").strip() or None,
    )


def _parse_keywords(raw: list) -> tuple[SkillKeyword, ...]:
    """Parse keyword list from frontmatter."""
    result: list[SkillKeyword] = []
    for item in raw:
        if isinstance(item, dict):
            result.append(SkillKeyword(
                keyword=str(item.get("keyword", "")),
                weight=float(item.get("weight", 1.0)),
            ))
        elif isinstance(item, str):
            result.append(SkillKeyword(keyword=item, weight=1.0))
    return tuple(result)


class SkillParser:
    """
    Stateless skill parser facade.

    Usage:
        parser = SkillParser()
        skill = parser.parse(Path("workspace/system/skills/foo/SKILL.md"))
    """

    @staticmethod
    def parse(path: Path) -> Skill:
        """Parse a SKILL.md file into a Skill object."""
        return parse_skill_file(path)
