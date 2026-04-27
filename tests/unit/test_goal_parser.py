# edition: baseline
"""
Tests for GOAL.md parser.
"""
from __future__ import annotations

import pytest

from src.worker.goal.parser import parse_goal
from src.worker.scripts.models import InlineScript


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_GOAL_MD = """---
goal_id: goal-001
title: Improve system reliability
status: active
priority: high
deadline: "2026-06-01"
created_by: admin
approved_by: manager
on_complete: create_duty
milestones:
  - id: ms-1
    title: Setup monitoring
    status: in_progress
    deadline: "2026-04-15"
    tasks:
      - id: t-1
        title: Install metrics collector
        status: completed
      - id: t-2
        title: Configure dashboards
        status: pending
        blocked_by:
          - t-1
  - id: ms-2
    title: Automate alerts
    status: pending
    deadline: "2026-05-01"
    tasks:
      - id: t-3
        title: Define alert rules
        status: pending
      - id: t-4
        title: Test alert pipeline
        status: pending
        blocked_by:
          - t-3
---
# Improve system reliability

Detailed description of the goal.
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestParseGoalValid:
    def test_basic_fields(self):
        goal = parse_goal(VALID_GOAL_MD)
        assert goal.goal_id == "goal-001"
        assert goal.title == "Improve system reliability"
        assert goal.status == "active"
        assert goal.priority == "high"
        assert goal.deadline == "2026-06-01"
        assert goal.created_by == "admin"
        assert goal.approved_by == "manager"
        assert goal.on_complete == "create_duty"
        assert goal.preferred_skill_ids == ()

    def test_milestones_parsed(self):
        goal = parse_goal(VALID_GOAL_MD)
        assert len(goal.milestones) == 2
        assert goal.milestones[0].id == "ms-1"
        assert goal.milestones[0].title == "Setup monitoring"
        assert goal.milestones[0].status == "in_progress"
        assert goal.milestones[1].id == "ms-2"

    def test_tasks_parsed(self):
        goal = parse_goal(VALID_GOAL_MD)
        ms1 = goal.milestones[0]
        assert len(ms1.tasks) == 2
        assert ms1.tasks[0].id == "t-1"
        assert ms1.tasks[0].status == "completed"
        assert ms1.tasks[1].id == "t-2"
        assert ms1.tasks[1].blocked_by == ("t-1",)

    def test_blocked_by_valid_references(self):
        """blocked_by references t-1 which exists - should not raise."""
        goal = parse_goal(VALID_GOAL_MD)
        assert goal.milestones[0].tasks[1].blocked_by == ("t-1",)

    def test_overall_progress(self):
        goal = parse_goal(VALID_GOAL_MD)
        # ms-1: 1/2 completed = 0.5, ms-2: 0/2 = 0.0
        # overall = (0.5 + 0.0) / 2 = 0.25
        assert goal.overall_progress == pytest.approx(0.25)

    def test_next_actionable_tasks(self):
        goal = parse_goal(VALID_GOAL_MD)
        actionable = goal.next_actionable_tasks
        ids = [t.id for t in actionable]
        # t-2 is pending, blocked_by t-1 (completed) -> actionable
        # t-3 is pending, no blocked_by -> actionable
        # t-4 is pending, blocked_by t-3 (pending) -> NOT actionable
        assert "t-2" in ids
        assert "t-3" in ids
        assert "t-4" not in ids

    def test_preferred_skill_ids_parsed(self):
        md = """---
goal_id: g-skills
title: Goal With Skills
status: active
priority: high
preferred_skill_ids:
  - approval-review
  - document-analysis
---
body
"""
        goal = parse_goal(md)
        assert goal.preferred_skill_ids == ("approval-review", "document-analysis")

    def test_skills_alias_parsed(self):
        md = """---
goal_id: g-skills-alias
title: Goal With Skills Alias
status: active
priority: high
skills:
  - approval-review
  - document-analysis
---
body
"""
        goal = parse_goal(md)
        assert goal.preferred_skill_ids == ("approval-review", "document-analysis")

    def test_default_pre_script_parsed(self):
        md = """---
goal_id: g-script
title: Goal With Script
status: active
priority: high
default_pre_script:
  kind: inline
  source: |
    print("prefetch")
  enabled_tools:
    - file_read
---
body
"""
        goal = parse_goal(md)
        assert isinstance(goal.default_pre_script, InlineScript)
        assert goal.default_pre_script.source.strip() == 'print("prefetch")'
        assert goal.default_pre_script.enabled_tools == ("file_read",)


class TestParseGoalValidation:
    def test_missing_frontmatter_raises(self):
        with pytest.raises(ValueError, match="missing YAML frontmatter"):
            parse_goal("No frontmatter here")

    def test_empty_goal_id_raises(self):
        md = """---
goal_id: ""
title: Test
status: active
priority: high
---
body
"""
        with pytest.raises(ValueError, match="goal_id must not be empty"):
            parse_goal(md)

    def test_invalid_status_raises(self):
        md = """---
goal_id: g1
title: Test
status: invalid_status
priority: high
---
body
"""
        with pytest.raises(ValueError, match="Invalid goal status"):
            parse_goal(md)

    def test_invalid_priority_raises(self):
        md = """---
goal_id: g1
title: Test
status: active
priority: critical
---
body
"""
        with pytest.raises(ValueError, match="Invalid priority"):
            parse_goal(md)

    def test_invalid_blocked_by_reference_raises(self):
        md = """---
goal_id: g1
title: Test
status: active
priority: high
milestones:
  - id: ms-1
    title: M1
    status: pending
    tasks:
      - id: t-1
        title: T1
        status: pending
        blocked_by:
          - nonexistent-task
---
body
"""
        with pytest.raises(ValueError, match="does not exist"):
            parse_goal(md)

    def test_invalid_task_status_raises(self):
        md = """---
goal_id: g1
title: Test
status: active
priority: high
milestones:
  - id: ms-1
    title: M1
    status: pending
    tasks:
      - id: t-1
        title: T1
        status: bad_status
---
body
"""
        with pytest.raises(ValueError, match="Invalid task status"):
            parse_goal(md)

    def test_invalid_milestone_status_raises(self):
        md = """---
goal_id: g1
title: Test
status: active
priority: high
milestones:
  - id: ms-1
    title: M1
    status: bad_status
---
body
"""
        with pytest.raises(ValueError, match="Invalid milestone status"):
            parse_goal(md)


class TestParseGoalMinimal:
    def test_minimal_goal(self):
        md = """---
goal_id: g-min
title: Minimal goal
status: active
priority: low
---
body
"""
        goal = parse_goal(md)
        assert goal.goal_id == "g-min"
        assert goal.milestones == ()
        assert goal.overall_progress == 0.0
        assert goal.external_source is None
        assert goal.on_complete is None
