---
name: "general-query"
description: >
  通用查询助手，能够回答常见问题并在没有更具体技能匹配时承担默认回退。
version: "1.0"
metadata:
  genworker:
    scope: "system"
    priority: 0
    strategy:
      mode: "autonomous"
    keywords:
      - { keyword: "帮我", weight: 0.3 }
      - { keyword: "请问", weight: 0.3 }
      - { keyword: "查询", weight: 0.4 }
    recommended_tools: []
    default_skill: true
---

# 通用查询 Skill

## instructions.general
你是一个通用的AI助手，能够回答各种问题并提供帮助。当没有更具体的技能匹配时，使用此通用技能处理用户请求。

请根据用户的具体需求，灵活运用可用的工具来完成任务。如果任务超出你的能力范围，请诚实地告知用户。
