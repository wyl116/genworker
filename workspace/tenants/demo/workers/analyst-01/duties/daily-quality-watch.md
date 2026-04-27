---
duty_id: daily-quality-watch
title: Daily Data Quality Watch
status: active
triggers:
  - id: morning-scan
    type: schedule
    cron: "0 9 * * *"
    description: Morning quality summary scan
  - id: alert-event
    type: event
    source: "external.data_quality_alert"
    description: React to upstream alert events
  - id: anomaly-threshold
    type: condition
    metric: anomaly_count
    rule: ">= 1"
    check_interval: "30m"
    description: Re-run investigation when recent anomalies exist
execution_policy:
  default: standard
  overrides:
    morning-scan: deep
    anomaly-threshold: quick
quality_criteria:
  - Confirm the affected dataset and business scope
  - Propose the next containment step when anomalies exist
  - Escalate to stakeholders if service impact is likely
escalation:
  condition: "critical"
  target: "data-team-lead"
---
Review the latest data quality indicators, summarize newly detected issues,
and decide whether the incident should trigger stakeholder communication.
