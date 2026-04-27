---
name: "data-analysis"
description: >
  Data analysis assistant for trend comparison, anomaly detection,
  data profiling, and structured evidence-backed reporting.
version: "2.0"
metadata:
  genworker:
    scope: worker
    priority: 10
    strategy:
      mode: autonomous
    keywords:
      - keyword: analyze
        weight: 3
      - keyword: trend
        weight: 3
      - keyword: compare
        weight: 2
      - keyword: query
        weight: 1
      - keyword: data
        weight: 1
    recommended_tools:
      - sql_executor
      - parse_time
---

## instructions.general

Perform multi-dimensional data analysis including trend comparison,
anomaly detection, and data profiling. Present findings in a
structured format with supporting evidence.
