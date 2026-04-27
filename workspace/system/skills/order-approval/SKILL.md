---
name: "order-approval"
description: "订单审批流程"
version: "1.0"
metadata:
  genworker:
    scope: "system"
    priority: 20
    strategy:
      mode: "langgraph"
      fallback:
        condition: "langgraph_unavailable"
        mode: "autonomous"
      graph:
        state_schema:
          order_id: "str"
          amount: "float"
          risk_score: "int"
          approved: "bool"
        entry: "fetch_order"
        nodes:
          - name: "fetch_order"
            kind: "tool"
            tool: "order_lookup"
          - name: "risk_check"
            kind: "llm"
            instruction_ref: "risk_check"
          - name: "need_human"
            kind: "condition"
            route:
              high_risk: "human_approval"
              low_risk: "auto_approve"
          - name: "human_approval"
            kind: "interrupt"
            prompt_ref: "approval_prompt"
            inbox_event_type: "order_approval"
          - name: "auto_approve"
            kind: "tool"
            tool: "order_approve"
          - name: "notify"
            kind: "llm"
            instruction_ref: "notify"
        edges:
          - { from: "fetch_order", to: "risk_check" }
          - { from: "risk_check", to: "need_human" }
          - { from: "need_human", to: "human_approval", cond: "high_risk" }
          - { from: "need_human", to: "auto_approve", cond: "low_risk" }
          - { from: "human_approval", to: "notify" }
          - { from: "auto_approve", to: "notify" }
          - { from: "notify", to: "END" }
    keywords:
      - { keyword: "审批", weight: 1.0 }
      - { keyword: "订单", weight: 0.8 }
---

## instructions.risk_check
根据当前状态判断 high_risk 或 low_risk，仅输出其中一个 route key。

## instructions.approval_prompt
订单 {order_id} 金额 {amount} 元，风险分 {risk_score}。请审批。
回复 /approve_confirmation {inbox_id} 通过 或 /reject_confirmation {inbox_id} 拒绝。

## instructions.notify
用一句话总结订单处理结果。
