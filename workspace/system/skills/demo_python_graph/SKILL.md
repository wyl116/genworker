---
name: "demo-python-graph"
description: "LangGraph Python factory demo"
version: "1.0"
metadata:
  genworker:
    scope: "system"
    priority: 5
    strategy:
      mode: "langgraph"
      fallback:
        condition: "langgraph_unavailable"
        mode: "autonomous"
      graph:
        module: "workspace.system.skills.demo_python_graph.graph"
        factory: "build_graph"
        state_schema_ref: "DemoState"
---

## instructions.general
运行 Python factory graph 示例。

## instructions.summary
总结当前图执行结果。
