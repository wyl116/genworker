---
name: "general-query"
description: >
  General query assistant for broad help, questions, and explanations
  when no more specific skill is a better fit.
version: "1.0"
metadata:
  genworker:
    scope: worker
    priority: 0
    default_skill: true
    strategy:
      mode: autonomous
    keywords:
      - keyword: help
        weight: 1
      - keyword: question
        weight: 1
      - keyword: explain
        weight: 1
---

## instructions.general

Handle general queries and questions that do not match a specific
specialized skill. Provide helpful, clear, and concise responses.
