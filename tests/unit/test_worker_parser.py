# edition: baseline
"""
Unit tests for PERSONA.md parser -> frozen Worker object.

Tests:
- Successful parsing with all fields
- Frozen immutability
- tool_policy uses frozenset
- Identity fields correctly parsed
- Invalid format raises WorkerException
- Missing worker_id raises WorkerException
"""
import dataclasses
import textwrap
from pathlib import Path

import pytest

from src.common.exceptions import WorkerException
from src.worker.models import (
    ServiceConfig,
    Worker,
    WorkerHeartbeatConfig,
    WorkerMode,
    WorkerToolPolicy,
)
from src.worker.parser import parse_persona_md


@pytest.fixture
def tmp_persona_md(tmp_path: Path) -> Path:
    """Create a valid PERSONA.md file in tmp_path."""
    content = textwrap.dedent("""\
        ---
        identity:
          name: "Test Worker"
          worker_id: "test-01"
          version: "1.0"
          role: "Test Role"
          department: "Test Dept"
          reports_to: "Test Leader"
          background: |
            A test worker background.
          personality:
            traits: [careful, proactive]
            communication_style: "Clear and direct"
            decision_style: "Data-driven"
          principles:
            - "Accuracy first"
            - "Always verify"

        tool_policy:
          mode: blacklist
          denied_tools:
            - db_write
            - admin_console

        skills_dir: "skills/"
        default_skill: "general-query"

        constraints:
          - "Must mask sensitive data"
          - "Max 10 dimensions per analysis"
        ---

        ## Work Guidelines

        This is the body content for system prompt injection.
    """)
    persona_md = tmp_path / "PERSONA.md"
    persona_md.write_text(content, encoding="utf-8")
    return persona_md


class TestParsePersonaMd:
    """Tests for parse_persona_md function."""

    def test_parse_frozen_worker_from_persona_md(self, tmp_persona_md: Path) -> None:
        """A valid PERSONA.md produces a frozen Worker with correct fields."""
        worker = parse_persona_md(tmp_persona_md)

        assert isinstance(worker, Worker)
        assert worker.worker_id == "test-01"
        assert worker.name == "Test Worker"
        assert worker.identity.role == "Test Role"
        assert worker.identity.department == "Test Dept"
        assert worker.identity.version == "1.0"
        assert worker.identity.reports_to == "Test Leader"
        assert worker.mode == WorkerMode.PERSONAL
        assert "test worker background" in worker.identity.background.lower()

    def test_worker_is_frozen(self, tmp_persona_md: Path) -> None:
        """Worker dataclass is immutable (frozen=True)."""
        worker = parse_persona_md(tmp_persona_md)

        with pytest.raises(dataclasses.FrozenInstanceError):
            worker.default_skill = "other"  # type: ignore[misc]

    def test_tool_policy_uses_frozenset(self, tmp_persona_md: Path) -> None:
        """WorkerToolPolicy uses frozenset for denied_tools and allowed_tools."""
        worker = parse_persona_md(tmp_persona_md)

        assert isinstance(worker.tool_policy.denied_tools, frozenset)
        assert isinstance(worker.tool_policy.allowed_tools, frozenset)
        assert worker.tool_policy.mode == "blacklist"
        assert "db_write" in worker.tool_policy.denied_tools
        assert "admin_console" in worker.tool_policy.denied_tools

    def test_identity_personality_parsed(self, tmp_persona_md: Path) -> None:
        """Personality traits, communication and decision styles are parsed."""
        worker = parse_persona_md(tmp_persona_md)

        personality = worker.identity.personality
        assert "careful" in personality.traits
        assert "proactive" in personality.traits
        assert personality.communication_style == "Clear and direct"
        assert personality.decision_style == "Data-driven"

    def test_principles_parsed(self, tmp_persona_md: Path) -> None:
        """Principles are parsed as a tuple of strings."""
        worker = parse_persona_md(tmp_persona_md)

        assert isinstance(worker.principles, tuple)
        assert len(worker.principles) == 2
        assert "Accuracy first" in worker.principles

    def test_constraints_parsed(self, tmp_persona_md: Path) -> None:
        """Constraints are parsed as a tuple of strings."""
        worker = parse_persona_md(tmp_persona_md)

        assert isinstance(worker.constraints, tuple)
        assert len(worker.constraints) == 2
        assert "Must mask sensitive data" in worker.constraints

    def test_body_instructions_parsed(self, tmp_persona_md: Path) -> None:
        """Markdown body after frontmatter is captured."""
        worker = parse_persona_md(tmp_persona_md)

        assert "Work Guidelines" in worker.body_instructions
        assert "system prompt injection" in worker.body_instructions

    def test_default_skill_parsed(self, tmp_persona_md: Path) -> None:
        """default_skill field is correctly parsed."""
        worker = parse_persona_md(tmp_persona_md)
        assert worker.default_skill == "general-query"

    def test_source_path_recorded(self, tmp_persona_md: Path) -> None:
        """source_path records the original file path."""
        worker = parse_persona_md(tmp_persona_md)
        assert str(tmp_persona_md) in worker.source_path

    def test_sensor_configs_parsed(self, tmp_path: Path) -> None:
        """sensor_configs are parsed as immutable mappings."""
        content = textwrap.dedent("""\
            ---
            identity:
              name: "Monitor Worker"
              worker_id: "monitor-01"
            sensor_configs:
              - source_type: email
                poll_interval: "15m"
                auto_create_goal: true
                require_approval: false
                filter:
                  subject_keywords: "Project"
            ---
            Body.
        """)
        persona_md = tmp_path / "PERSONA.md"
        persona_md.write_text(content, encoding="utf-8")

        worker = parse_persona_md(persona_md)

        assert len(worker.sensor_configs) == 1
        sensor = worker.sensor_configs[0]
        assert sensor["source_type"] == "email"
        assert sensor["poll_interval"] == "15m"
        assert sensor["auto_create_goal"] is True

    def test_monitor_configs_supported_as_legacy_alias(self, tmp_path: Path) -> None:
        """monitor_configs remains compatible with the sensor pipeline."""
        content = textwrap.dedent("""\
            ---
            identity:
              name: "Legacy Monitor Worker"
              worker_id: "legacy-monitor-01"
            monitor_configs:
              - source_type: webhook
                auto_create_goal: true
                require_approval: false
                filter:
                  event_type: "external.alert"
            ---
            Body.
        """)
        persona_md = tmp_path / "PERSONA.md"
        persona_md.write_text(content, encoding="utf-8")

        worker = parse_persona_md(persona_md)

        assert len(worker.sensor_configs) == 1
        sensor = worker.sensor_configs[0]
        assert sensor["source_type"] == "webhook"
        assert sensor["auto_create_goal"] is True
        assert sensor["filter"]["event_type"] == "external.alert"

    def test_channels_parsed(self, tmp_path: Path) -> None:
        """channels are parsed as immutable mappings."""
        content = textwrap.dedent("""\
            ---
            identity:
              name: "Channel Worker"
              worker_id: "channel-01"
            channels:
              - type: feishu
                connection_mode: webhook
                chat_ids:
                  - oc_123
                reply_mode: complete
                features:
                  monitor_group_chat: true
            ---
            Body.
        """)
        persona_md = tmp_path / "PERSONA.md"
        persona_md.write_text(content, encoding="utf-8")

        worker = parse_persona_md(persona_md)

        assert len(worker.channels) == 1
        channel = worker.channels[0]
        assert channel["type"] == "feishu"
        assert channel["connection_mode"] == "webhook"
        assert channel["chat_ids"] == ["oc_123"]
        assert channel["features"]["monitor_group_chat"] is True

    def test_explicit_team_member_mode_parsed(self, tmp_path: Path) -> None:
        """Top-level mode is parsed and validated."""
        content = textwrap.dedent("""\
            ---
            identity:
              name: "Team Worker"
              worker_id: "team-01"
            mode: team_member
            ---
            Body.
        """)
        persona_md = tmp_path / "PERSONA.md"
        persona_md.write_text(content, encoding="utf-8")

        worker = parse_persona_md(persona_md)
        assert worker.mode == WorkerMode.TEAM_MEMBER

    def test_service_mode_parses_service_config(self, tmp_path: Path) -> None:
        """Service-mode configuration is parsed into ServiceConfig."""
        content = textwrap.dedent("""\
            ---
            identity:
              name: "Service Worker"
              worker_id: "svc-01"
            mode: service
            service:
              knowledge_sources:
                - type: docs_folder
                  path: mounts://knowledge_base/
              session_ttl: 900
              max_concurrent_sessions: 80
              anonymous_allowed: false
              escalation:
                enabled: true
                target_worker: human_support
                triggers:
                  - "用户要求转人工"
                  - "连续3轮未解决"
            ---
            Body.
        """)
        persona_md = tmp_path / "PERSONA.md"
        persona_md.write_text(content, encoding="utf-8")

        worker = parse_persona_md(persona_md)
        assert worker.mode == WorkerMode.SERVICE
        assert isinstance(worker.service_config, ServiceConfig)
        assert worker.service_config is not None
        assert worker.service_config.session_ttl == 900
        assert worker.service_config.max_concurrent_sessions == 80
        assert worker.service_config.anonymous_allowed is False
        assert worker.service_config.escalation_enabled is True
        assert worker.service_config.escalation_target == "human_support"
        assert "用户要求转人工" in worker.service_config.escalation_triggers

    def test_heartbeat_config_parsed(self, tmp_path: Path) -> None:
        """Worker heartbeat overrides are parsed from PERSONA.md."""
        content = textwrap.dedent("""\
            ---
            identity:
              name: "Heartbeat Worker"
              worker_id: "hb-01"
            heartbeat:
              goal_task_actions:
                - escalate
                - replan
              goal_isolated_actions:
                - deep_review
              goal_isolated_deviation_threshold: 0.97
            ---
            Body.
        """)
        persona_md = tmp_path / "PERSONA.md"
        persona_md.write_text(content, encoding="utf-8")

        worker = parse_persona_md(persona_md)

        assert isinstance(worker.heartbeat_config, WorkerHeartbeatConfig)
        assert worker.heartbeat_config.goal_task_actions == ("escalate", "replan")
        assert worker.heartbeat_config.goal_isolated_actions == ("deep_review",)
        assert worker.heartbeat_config.goal_isolated_deviation_threshold == 0.97


class TestParsePersonaMdErrors:
    """Tests for error handling in PERSONA.md parsing."""

    def test_invalid_persona_md_raises_parse_error(self, tmp_path: Path) -> None:
        """Missing YAML frontmatter delimiters raises WorkerException."""
        bad_file = tmp_path / "PERSONA.md"
        bad_file.write_text("no frontmatter here", encoding="utf-8")

        with pytest.raises(WorkerException, match="missing YAML frontmatter"):
            parse_persona_md(bad_file)

    def test_missing_worker_id_raises_error(self, tmp_path: Path) -> None:
        """Missing identity.worker_id raises WorkerException."""
        content = textwrap.dedent("""\
            ---
            identity:
              name: "No ID Worker"
            ---
            Body text.
        """)
        bad_file = tmp_path / "PERSONA.md"
        bad_file.write_text(content, encoding="utf-8")

        with pytest.raises(WorkerException, match="worker_id"):
            parse_persona_md(bad_file)

    def test_invalid_yaml_raises_error(self, tmp_path: Path) -> None:
        """Malformed YAML raises WorkerException."""
        content = "---\n[invalid: yaml: content:\n---\nBody.\n"
        bad_file = tmp_path / "PERSONA.md"
        bad_file.write_text(content, encoding="utf-8")

        with pytest.raises(WorkerException, match="Invalid YAML"):
            parse_persona_md(bad_file)

    def test_nonexistent_file_raises_error(self, tmp_path: Path) -> None:
        """Missing file raises WorkerException."""
        missing = tmp_path / "nonexistent" / "PERSONA.md"

        with pytest.raises(WorkerException, match="not found"):
            parse_persona_md(missing)

    def test_non_mapping_yaml_raises_error(self, tmp_path: Path) -> None:
        """YAML that is not a mapping raises WorkerException."""
        content = "---\n- just a list\n---\nBody.\n"
        bad_file = tmp_path / "PERSONA.md"
        bad_file.write_text(content, encoding="utf-8")

        with pytest.raises(WorkerException, match="not a mapping"):
            parse_persona_md(bad_file)

    def test_invalid_mode_raises_error(self, tmp_path: Path) -> None:
        """Unknown top-level mode raises WorkerException."""
        content = textwrap.dedent("""\
            ---
            identity:
              name: "Bad Mode Worker"
              worker_id: "bad-mode-01"
            mode: vendor
            ---
            Body.
        """)
        bad_file = tmp_path / "PERSONA.md"
        bad_file.write_text(content, encoding="utf-8")

        with pytest.raises(WorkerException, match="Invalid PERSONA.md mode"):
            parse_persona_md(bad_file)


class TestWhitelistToolPolicy:
    """Tests for whitelist mode tool policy."""

    def test_whitelist_mode_parsed(self, tmp_path: Path) -> None:
        """Whitelist mode with allowed_tools is correctly parsed."""
        content = textwrap.dedent("""\
            ---
            identity:
              name: "Whitelist Worker"
              worker_id: "wl-01"
            tool_policy:
              mode: whitelist
              allowed_tools:
                - sql_executor
                - data_profiler
            ---
            Body.
        """)
        persona_md = tmp_path / "PERSONA.md"
        persona_md.write_text(content, encoding="utf-8")

        worker = parse_persona_md(persona_md)
        assert worker.tool_policy.mode == "whitelist"
        assert isinstance(worker.tool_policy.allowed_tools, frozenset)
        assert "sql_executor" in worker.tool_policy.allowed_tools
        assert "data_profiler" in worker.tool_policy.allowed_tools
