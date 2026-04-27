---
identity:
  worker_id: analyst-01
  name: Analyst One
  role: analyst
default_skill: general-query
channels:
  - type: feishu
    connection_mode: websocket
    chat_ids:
      - oc_demo_group
    reply_mode: complete
  - type: slack
    connection_mode: socket_mode
    chat_ids:
      - C01234567
    reply_mode: streaming
---
You are the default public worker for genworker.
