"""Build compiled langgraph instances from skill strategy definitions."""
from __future__ import annotations

import importlib
from dataclasses import fields, is_dataclass
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from src.common.exceptions import SkillException
from src.skills.models import GraphDefinition, NodeKind, Skill

from .context import NodeContext
from .models import BudgetTracker, CompiledGraphBundle, LangGraphInitError
from .node_handlers import build_condition_router, build_node_handler

_ALLOWED_MODULE_PREFIXES = (
    "workspace.system.skills.",
    "workspace.tenants.",
    "src.",
)

_TYPE_MAP = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "dict": dict,
    "list": list,
    "any": Any,
}


def build_graph_bundle(
    *,
    skill: Skill,
    node_context: NodeContext,
    budget_tracker: BudgetTracker,
) -> CompiledGraphBundle:
    """Build a compiled graph bundle from the skill strategy."""
    graph = skill.strategy.graph
    if graph is None:
        raise LangGraphInitError("Skill has no graph definition")
    if graph.source == "yaml":
        return _build_yaml_graph(
            skill=skill,
            graph=graph,
            node_context=node_context,
            budget_tracker=budget_tracker,
        )
    if graph.source == "python":
        return _build_python_graph(
            skill=skill,
            graph=graph,
            node_context=node_context,
        )
    raise LangGraphInitError(f"Unsupported graph source '{graph.source}'")


def _build_yaml_graph(
    *,
    skill: Skill,
    graph: GraphDefinition,
    node_context: NodeContext,
    budget_tracker: BudgetTracker,
) -> CompiledGraphBundle:
    state_type = _build_state_type(skill.skill_id, graph)
    builder = StateGraph(state_type)
    node_index = {node.name: node for node in graph.nodes}
    interrupt_nodes = {
        node.name: node for node in graph.nodes if node.kind == NodeKind.INTERRUPT
    }
    for node in graph.nodes:
        builder.add_node(
            node.name,
            build_node_handler(
                node,
                ctx=node_context,
                budget_tracker=budget_tracker,
            ),
        )

    builder.set_entry_point(graph.entry)

    conditional_edges: dict[str, dict[str, Any]] = {}
    for edge in graph.edges:
        target = END if edge.to_node == "END" else edge.to_node
        if edge.cond:
            conditional_edges.setdefault(edge.from_node, {})[edge.cond] = target
            continue
        builder.add_edge(edge.from_node, target)

    for node_name, route_map in conditional_edges.items():
        node = node_index[node_name]
        builder.add_conditional_edges(
            node_name,
            build_condition_router(node),
            route_map,
        )

    compiled = builder.compile(
        checkpointer=node_context.checkpointer,
        interrupt_before=sorted(interrupt_nodes),
    )
    return CompiledGraphBundle(
        skill=skill,
        compiled=compiled,
        state_whitelist=tuple(graph.state_schema.keys()),
        interrupt_nodes=interrupt_nodes,
        max_steps=max(int(graph.max_steps or 50), 1),
    )


def _build_python_graph(
    *,
    skill: Skill,
    graph: GraphDefinition,
    node_context: NodeContext,
) -> CompiledGraphBundle:
    if not graph.module.startswith(_ALLOWED_MODULE_PREFIXES):
        raise LangGraphInitError(
            f"Python graph module '{graph.module}' is outside allowed prefixes"
        )
    try:
        module = importlib.import_module(graph.module)
    except ImportError as exc:
        raise LangGraphInitError(str(exc)) from exc
    factory = getattr(module, graph.factory, None)
    if factory is None:
        raise LangGraphInitError(
            f"Python graph factory '{graph.factory}' not found in '{graph.module}'"
        )
    compiled = factory(node_context)
    if hasattr(compiled, "compile") and not hasattr(compiled, "astream_events"):
        compiled = compiled.compile(checkpointer=node_context.checkpointer)
    if not hasattr(compiled, "astream_events"):
        raise LangGraphInitError(
            f"Python graph factory '{graph.factory}' did not return a compiled graph"
        )
    whitelist = _resolve_python_whitelist(module, graph.state_schema_ref)
    return CompiledGraphBundle(
        skill=skill,
        compiled=compiled,
        state_whitelist=whitelist,
        interrupt_nodes={},
        max_steps=max(int(graph.max_steps or 50), 1),
    )


def _build_state_type(skill_id: str, graph: GraphDefinition):
    annotations: dict[str, Any] = {
        key: _resolve_annotation(type_name)
        for key, type_name in graph.state_schema.items()
    }
    annotations["_approval_decision"] = dict[str, Any]
    annotations["_approval_inbox_id"] = str
    annotations["_last_output"] = str
    type_name = "".join(part.capitalize() for part in skill_id.replace("-", "_").split("_")) or "LangGraphState"
    return TypedDict(type_name, annotations, total=False)


def _resolve_annotation(raw: str) -> Any:
    return _TYPE_MAP.get(str(raw).strip().lower(), Any)


def _resolve_python_whitelist(module: Any, state_schema_ref: str) -> tuple[str, ...]:
    if not state_schema_ref:
        return ()
    state_type = getattr(module, state_schema_ref, None)
    if state_type is None:
        raise LangGraphInitError(f"state_schema_ref '{state_schema_ref}' not found")
    if is_dataclass(state_type):
        return tuple(field.name for field in fields(state_type))
    annotations = getattr(state_type, "__annotations__", {})
    if isinstance(annotations, dict):
        return tuple(str(key) for key in annotations.keys())
    raise LangGraphInitError(
        f"state_schema_ref '{state_schema_ref}' has no dataclass fields or annotations"
    )
