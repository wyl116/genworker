---
goal_id: goal-demo-data-quality
title: Restore daily data quality SLA
status: active
priority: high
deadline: "2026-04-15"
external_source:
  type: email
  source_uri: email://alerts/data-quality-sla
  sync_direction: bidirectional
  sync_schedule: 30m
  stakeholders:
    - data-team-lead@company.com
    - platform-owner@company.com
milestones:
  - id: ms-1
    title: Validate scope and impact
    status: in_progress
    deadline: "2026-04-05"
    tasks:
      - id: ms-1-t1
        title: Confirm affected tables
        status: completed
      - id: ms-1-t2
        title: Quantify downstream report impact
        status: in_progress
  - id: ms-2
    title: Ship remediation plan
    status: pending
    deadline: "2026-04-10"
    tasks:
      - id: ms-2-t1
        title: Publish remediation owner list
        status: pending
        blocked_by:
          - ms-1-t2
      - id: ms-2-t2
        title: Send stakeholder ETA update
        status: pending
        blocked_by:
          - ms-2-t1
---
# Restore daily data quality SLA

Drive remediation for the current SLA regression and keep stakeholders aligned
through the external sync channel.
