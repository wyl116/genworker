# edition: baseline
from pathlib import Path

import pytest

from src.common.exceptions import SkillException
from src.skills.models import StrategyMode
from src.skills.parser import SkillParser


def _write_skill(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "demo" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_parse_yaml_langgraph_skill(tmp_path: Path):
    path = _write_skill(
        tmp_path,
        """\
---
skill_id: "yaml-graph"
strategy:
  mode: "langgraph"
  graph:
    state_schema:
      task: "str"
    entry: "start"
    nodes:
      - name: "start"
        kind: "llm"
        instruction_ref: "general"
      - name: "gate"
        kind: "condition"
        route:
          go: "END"
    edges:
      - { from: "start", to: "gate" }
      - { from: "gate", to: "END", cond: "go" }
---

## instructions.general
hello
""",
    )

    skill = SkillParser.parse(path)

    assert skill.strategy.mode == StrategyMode.LANGGRAPH
    assert skill.strategy.graph is not None
    assert skill.strategy.graph.source == "yaml"
    assert skill.strategy.graph.entry == "start"
    assert skill.strategy.graph.edges[1].to_node == "END"


def test_parse_python_langgraph_skill(tmp_path: Path):
    path = _write_skill(
        tmp_path,
        """\
---
name: "python-graph"
metadata:
  genworker:
    strategy:
      mode: "langgraph"
      graph:
        module: "workspace.system.skills.demo_python_graph.graph"
        factory: "build_graph"
        state_schema_ref: "DemoState"
---
body
""",
    )

    skill = SkillParser.parse(path)

    assert skill.strategy.mode == StrategyMode.LANGGRAPH
    assert skill.strategy.graph is not None
    assert skill.strategy.graph.source == "python"
    assert skill.strategy.graph.module == "workspace.system.skills.demo_python_graph.graph"


@pytest.mark.parametrize(
    "body, message",
    [
        (
            """\
---
skill_id: "missing-graph"
strategy:
  mode: "langgraph"
---
""",
            "requires strategy.graph",
        ),
        (
            """\
---
skill_id: "mixed-graph"
strategy:
  mode: "langgraph"
  graph:
    entry: "start"
    nodes: []
    module: "workspace.system.skills.demo_python_graph.graph"
    factory: "build_graph"
---
""",
            "cannot mix YAML and Python sources",
        ),
        (
            """\
---
skill_id: "bad-entry"
strategy:
  mode: "langgraph"
  graph:
    entry: "missing"
    nodes:
      - name: "start"
        kind: "llm"
    edges: []
---
""",
            "entry 'missing'",
        ),
        (
            """\
---
skill_id: "bad-edge"
strategy:
  mode: "langgraph"
  graph:
    entry: "start"
    nodes:
      - name: "start"
        kind: "llm"
    edges:
      - { from: "start", to: "missing" }
---
""",
            "unknown to_node 'missing'",
        ),
    ],
)
def test_parse_langgraph_errors(tmp_path: Path, body: str, message: str):
    path = _write_skill(tmp_path, body)

    with pytest.raises(SkillException, match=message):
        SkillParser.parse(path)
