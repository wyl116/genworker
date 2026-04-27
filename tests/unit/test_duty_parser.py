# edition: baseline
"""
Tests for DUTY.md parser - field extraction, validation, error cases.
"""
import pytest

from src.worker.duty.models import Duty, DutyTrigger, ExecutionPolicy
from src.worker.duty.parser import DutyParseError, parse_duty
from src.worker.scripts.models import InlineScript


VALID_DUTY_MD = """---
duty_id: monitor-data-quality
title: Data Quality Monitor
status: active
triggers:
  - id: daily-check
    type: schedule
    description: Daily data quality check
    cron: "0 9 * * *"
  - id: on-upload
    type: event
    description: Check on file upload
    source: data.file_uploaded
    filter:
      type: csv
execution_policy:
  default: standard
  overrides:
    daily-check: deep
quality_criteria:
  - All critical fields must have values
  - Data formats must match schema
skill_hint: data-analysis
escalation:
  condition: anomaly_detected
  target: admin-team
execution_log_retention: "30d"
---

Check data quality across all active data sources.
Validate schemas, detect anomalies, and report findings.
"""


MINIMAL_DUTY_MD = """---
duty_id: simple-task
title: Simple Task
triggers:
  - id: manual-trigger
    type: manual
quality_criteria:
  - Task must complete
---

Execute the simple task.
"""


class TestParseDutyValid:
    def test_full_duty_parsing(self):
        duty = parse_duty(VALID_DUTY_MD)
        assert duty.duty_id == "monitor-data-quality"
        assert duty.title == "Data Quality Monitor"
        assert duty.status == "active"
        assert len(duty.triggers) == 2
        assert duty.skill_hint == "data-analysis"
        assert duty.skill_id == "data-analysis"
        assert duty.preferred_skill_id == "data-analysis"
        assert duty.execution_log_retention == "30d"

    def test_triggers_parsed_correctly(self):
        duty = parse_duty(VALID_DUTY_MD)

        schedule_trigger = duty.triggers[0]
        assert schedule_trigger.id == "daily-check"
        assert schedule_trigger.type == "schedule"
        assert schedule_trigger.cron == "0 9 * * *"

        event_trigger = duty.triggers[1]
        assert event_trigger.id == "on-upload"
        assert event_trigger.type == "event"
        assert event_trigger.source == "data.file_uploaded"
        assert event_trigger.filter == (("type", "csv"),)

    def test_execution_policy(self):
        duty = parse_duty(VALID_DUTY_MD)
        assert duty.execution_policy.default == "standard"
        assert duty.execution_policy.overrides == (("daily-check", "deep"),)

    def test_quality_criteria(self):
        duty = parse_duty(VALID_DUTY_MD)
        assert len(duty.quality_criteria) == 2
        assert "All critical fields must have values" in duty.quality_criteria

    def test_escalation_policy(self):
        duty = parse_duty(VALID_DUTY_MD)
        assert duty.escalation is not None
        assert duty.escalation.condition == "anomaly_detected"
        assert duty.escalation.target == "admin-team"

    def test_action_from_body(self):
        duty = parse_duty(VALID_DUTY_MD)
        assert "Check data quality" in duty.action

    def test_minimal_duty(self):
        duty = parse_duty(MINIMAL_DUTY_MD)
        assert duty.duty_id == "simple-task"
        assert duty.status == "active"
        assert len(duty.triggers) == 1
        assert duty.triggers[0].type == "manual"
        assert duty.escalation is None
        assert duty.execution_policy.default == "standard"
        assert duty.skill_id is None
        assert duty.preferred_skill_id is None

    def test_skill_id_binding_parses(self):
        content = """---
duty_id: skill-bound-duty
title: Skill Bound Duty
triggers:
  - id: t1
    type: manual
quality_criteria:
  - done
skill_id: structured-review
---
Review the structured payload.
"""
        duty = parse_duty(content)
        assert duty.skill_id == "structured-review"
        assert duty.skill_hint == "structured-review"
        assert duty.preferred_skill_id == "structured-review"

    def test_explicit_skill_id_overrides_legacy_hint(self):
        content = """---
duty_id: dual-skill-duty
title: Dual Skill Duty
triggers:
  - id: t1
    type: manual
quality_criteria:
  - done
skill_id: approval-review
skill_hint: legacy-hint
---
Review the approval request.
"""
        duty = parse_duty(content)
        assert duty.skill_id == "approval-review"
        assert duty.skill_hint == "legacy-hint"
        assert duty.preferred_skill_id == "approval-review"

    def test_preferred_skill_ids_parse_from_alias(self):
        content = """---
duty_id: preferred-duty
title: Preferred Duty
triggers:
  - id: t1
    type: manual
quality_criteria:
  - done
skills:
  - approval-review
  - document-analysis
---
Review the incoming request.
"""
        duty = parse_duty(content)
        assert duty.preferred_skill_ids == ("approval-review", "document-analysis")
        assert duty.soft_preferred_skill_ids == ("approval-review", "document-analysis")

    def test_depth_for_trigger_with_override(self):
        duty = parse_duty(VALID_DUTY_MD)
        assert duty.depth_for_trigger("daily-check") == "deep"

    def test_depth_for_trigger_default(self):
        duty = parse_duty(VALID_DUTY_MD)
        assert duty.depth_for_trigger("on-upload") == "standard"

    def test_condition_trigger_default_check_interval(self):
        content = """---
duty_id: cond-test
title: Condition Test
triggers:
  - id: cond-1
    type: condition
    metric: error_rate
    rule: "> 0.1"
quality_criteria:
  - Check passes
---
Check condition.
"""
        duty = parse_duty(content)
        assert duty.triggers[0].check_interval == "5m"

    def test_condition_trigger_custom_check_interval(self):
        content = """---
duty_id: cond-test-2
title: Condition Test 2
triggers:
  - id: cond-2
    type: condition
    metric: error_rate
    rule: "> 0.1"
    check_interval: "10m"
quality_criteria:
  - Check passes
---
Check condition with custom interval.
"""
        duty = parse_duty(content)
        assert duty.triggers[0].check_interval == "10m"

    def test_pre_script_inline_block_parses(self):
        content = """---
duty_id: scripted-duty
title: Scripted Duty
triggers:
  - id: t1
    type: manual
quality_criteria:
  - done
pre_script:
  source: |
    print("prepared")
  enabled_tools:
    - read_file
  timeout_seconds: 120
---
Run the scripted task.
"""
        duty = parse_duty(content)

        assert isinstance(duty.pre_script, InlineScript)
        assert duty.pre_script.source.strip() == 'print("prepared")'
        assert duty.pre_script.enabled_tools == ("read_file",)
        assert duty.pre_script.timeout_seconds == 120


class TestParseDutyErrors:
    def test_missing_duty_id(self):
        content = """---
title: No ID
triggers:
  - id: t1
    type: manual
quality_criteria:
  - ok
---
action
"""
        with pytest.raises(DutyParseError, match="duty_id"):
            parse_duty(content)

    def test_missing_title(self):
        content = """---
duty_id: no-title
triggers:
  - id: t1
    type: manual
quality_criteria:
  - ok
---
action
"""
        with pytest.raises(DutyParseError, match="title"):
            parse_duty(content)

    def test_no_triggers(self):
        content = """---
duty_id: no-triggers
title: No Triggers
triggers: []
quality_criteria:
  - ok
---
action
"""
        with pytest.raises(DutyParseError, match="trigger"):
            parse_duty(content)

    def test_invalid_trigger_type(self):
        content = """---
duty_id: bad-type
title: Bad Type
triggers:
  - id: t1
    type: invalid_type
quality_criteria:
  - ok
---
action
"""
        with pytest.raises(DutyParseError, match="not allowed"):
            parse_duty(content)

    def test_schedule_without_cron(self):
        content = """---
duty_id: no-cron
title: No Cron
triggers:
  - id: t1
    type: schedule
quality_criteria:
  - ok
---
action
"""
        with pytest.raises(DutyParseError, match="cron"):
            parse_duty(content)

    def test_event_without_source(self):
        content = """---
duty_id: no-source
title: No Source
triggers:
  - id: t1
    type: event
quality_criteria:
  - ok
---
action
"""
        with pytest.raises(DutyParseError, match="source"):
            parse_duty(content)

    def test_condition_without_metric(self):
        content = """---
duty_id: no-metric
title: No Metric
triggers:
  - id: t1
    type: condition
    rule: "> 0.1"
quality_criteria:
  - ok
---
action
"""
        with pytest.raises(DutyParseError, match="metric"):
            parse_duty(content)

    def test_condition_without_rule(self):
        content = """---
duty_id: no-rule
title: No Rule
triggers:
  - id: t1
    type: condition
    metric: error_rate
quality_criteria:
  - ok
---
action
"""
        with pytest.raises(DutyParseError, match="rule"):
            parse_duty(content)

    def test_no_quality_criteria(self):
        content = """---
duty_id: no-criteria
title: No Criteria
triggers:
  - id: t1
    type: manual
quality_criteria: []
---
action
"""
        with pytest.raises(DutyParseError, match="quality criterion"):
            parse_duty(content)

    def test_invalid_yaml(self):
        content = "not valid yaml frontmatter"
        # python-frontmatter handles this gracefully, but there's no duty_id
        with pytest.raises(DutyParseError):
            parse_duty(content)

    def test_invalid_execution_depth(self):
        content = """---
duty_id: bad-depth
title: Bad Depth
triggers:
  - id: t1
    type: manual
execution_policy:
  default: ultra
quality_criteria:
  - ok
---
action
"""
        with pytest.raises(DutyParseError, match="not allowed"):
            parse_duty(content)

    def test_collaboration_trigger_parses(self):
        content = """---
duty_id: collab
title: Collaboration Test
triggers:
  - id: c1
    type: collaboration
    description: Cross-team task
quality_criteria:
  - done
---
Collaborate.
"""
        duty = parse_duty(content)
        assert duty.triggers[0].type == "collaboration"
