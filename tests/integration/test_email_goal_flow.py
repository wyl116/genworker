# edition: baseline
"""
Integration test: email -> parse -> goal creation -> sync flow.

Verifies the end-to-end flow from email detection through
ContentParser to GoalGenerator and SyncManager.
"""
from __future__ import annotations

import json

import pytest

from src.channels.outbound_types import ChannelMessage
from src.events.bus import EventBus, Subscription
from src.events.models import Event
from src.worker.integrations.content_parser import ContentParser
from src.worker.integrations.goal_generator import generate_goal_from_parsed
from src.worker.integrations.sync_manager import SyncManager
from src.worker.sensing.sensors.email_sensor import EmailSensor, _filter_emails


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------

class MockLLMClient:
    """Mock LLM that returns a valid goal extraction."""

    def __init__(self, parsed_data: dict | None = None) -> None:
        self._data = parsed_data or {
            "title": "Q2 Data Migration",
            "description": "Migrate all data to new platform by June",
            "milestones": [
                {
                    "title": "Design",
                    "deadline": "2026-04-15",
                    "tasks": [{"title": "Write spec"}],
                },
                {
                    "title": "Implementation",
                    "deadline": "2026-05-30",
                    "tasks": [{"title": "Build pipeline"}],
                },
            ],
            "deadline": "2026-06-30",
            "priority": "high",
            "stakeholders": ["leader@company.com", "dev@company.com"],
            "confidence": 0.85,
        }

    async def invoke(self, messages, **kwargs):
        return json.dumps(self._data)


class MockToolExecutor:
    """Mock tool executor for email_search and email_send."""

    def __init__(self, emails: list[dict] | None = None) -> None:
        self._emails = emails or []
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, tool_name: str, tool_input: dict):
        self.calls.append((tool_name, tool_input))
        if tool_name == "email_search":
            return self._emails
        if tool_name == "email_send":
            return {"status": "sent", "id": "email-out-1"}
        return {}


class MockChannelAdapter:
    """Mock channel for SyncManager."""

    def __init__(self):
        self.sent: list[ChannelMessage] = []

    async def send(self, message: ChannelMessage) -> str:
        self.sent.append(message)
        return f"msg-{len(self.sent)}"

    async def update_document(self, path, content, section=None, *, scope=None) -> bool:
        return True


# ---------------------------------------------------------------------------
# Test: Full email -> goal flow
# ---------------------------------------------------------------------------

class TestEmailGoalFlow:
    @pytest.mark.asyncio
    async def test_email_detection_and_filtering(self):
        """EmailSensor helper filters emails by subject keywords."""
        emails = [
            {"subject": "Q2 Data Migration Project Plan", "from": "boss@company.com", "content": "Details..."},
            {"subject": "Lunch tomorrow?", "from": "friend@gmail.com", "content": "Let's eat"},
            {"subject": "Task assignment: review code", "from": "lead@company.com", "content": "Please review"},
        ]
        filter_dict = {
            "subject_keywords": "Project,Task",
            "from_domains": "company.com",
        }

        matched = _filter_emails(emails, filter_dict)

        assert len(matched) == 2
        assert matched[0]["subject"] == "Q2 Data Migration Project Plan"
        assert matched[1]["subject"] == "Task assignment: review code"

    @pytest.mark.asyncio
    async def test_content_parser_extracts_goal_info(self):
        """ContentParser extracts ParsedGoalInfo from email content."""
        llm = MockLLMClient()
        parser = ContentParser(llm, confidence_threshold=0.6)

        result = await parser.parse(
            content="We need to complete Q2 data migration by June...",
            source_type="email",
            context={
                "source_uri": "email://inbox/42",
                "worker_name": "Assistant",
            },
        )

        assert result is not None
        assert result.title == "Q2 Data Migration"
        assert result.confidence == 0.85
        assert len(result.milestones) == 2

    @pytest.mark.asyncio
    async def test_goal_generation_from_parsed(self, tmp_path):
        """ParsedGoalInfo -> Goal + GOAL.md + event."""
        llm = MockLLMClient()
        parser = ContentParser(llm, confidence_threshold=0.6)
        event_bus = EventBus()

        # Track events
        created_events: list[Event] = []

        async def on_goal_created(event: Event):
            created_events.append(event)

        event_bus.subscribe(Subscription(
            handler_id="test-handler",
            event_type="goal.created_from_external",
            tenant_id="test",
            handler=on_goal_created,
        ))

        parsed = await parser.parse(
            content="Q2 migration project details...",
            source_type="email",
            context={"source_uri": "email://inbox/42"},
        )

        assert parsed is not None

        goals_dir = tmp_path / "goals"
        goal = await generate_goal_from_parsed(
            parsed=parsed,
            goals_dir=goals_dir,
            require_approval=True,
            event_bus=event_bus,
            tenant_id="test",
        )

        # Verify goal
        assert goal.status == "pending_approval"
        assert goal.external_source is not None
        assert goal.external_source.type == "email"
        assert "leader@company.com" in goal.external_source.stakeholders

        # Verify GOAL.md written
        files = list(goals_dir.glob("*.md"))
        assert len(files) == 1

        # Verify event published
        assert len(created_events) == 1
        payload = dict(created_events[0].payload)
        assert payload["source_type"] == "email"
        assert payload["require_approval"] is True

    @pytest.mark.asyncio
    async def test_sync_sends_progress_email(self, tmp_path):
        """SyncManager sends progress email to stakeholders."""
        llm = MockLLMClient()
        parser = ContentParser(llm, confidence_threshold=0.6)

        parsed = await parser.parse(
            content="project details",
            source_type="email",
            context={"source_uri": "email://inbox/42"},
        )
        assert parsed is not None

        goal = await generate_goal_from_parsed(
            parsed=parsed,
            goals_dir=tmp_path / "goals",
            require_approval=False,
        )

        channel = MockChannelAdapter()
        sync_mgr = SyncManager(channel)

        record = await sync_mgr.sync_goal_progress(
            goal,
            tenant_id="test",
            worker_id="worker-1",
        )

        assert record.status == "success"
        assert record.direction == "outbound"
        assert len(channel.sent) == 1
        msg = channel.sent[0]
        assert "leader@company.com" in msg.recipients
        assert "Q2 Data Migration" in msg.subject
        assert msg.sender_tenant_id == "test"
        assert msg.sender_worker_id == "worker-1"

    @pytest.mark.asyncio
    async def test_full_pipeline_email_to_sync(self, tmp_path):
        """End-to-end: email detection -> parse -> goal -> sync."""
        # 1. Setup
        emails = [
            {
                "subject": "New Project: Q2 Migration",
                "from": "boss@company.com",
                "content": "We need to migrate data by June...",
            },
        ]
        tool_executor = MockToolExecutor(emails)
        event_bus = EventBus()
        llm = MockLLMClient()

        # 2. Email sensor detects email
        sensor = EmailSensor(
            tool_executor=tool_executor,
            filter_config={"subject_keywords": "Project"},
        )
        facts = await sensor.poll()
        assert len(facts) == 1
        matched = facts[0].payload_dict

        # 3. ContentParser parses matched email
        parser = ContentParser(llm, confidence_threshold=0.6)
        parsed = await parser.parse(
            content=matched["content"],
            source_type="email",
            context={"source_uri": f"email://inbox/{matched['subject']}"},
        )
        assert parsed is not None

        # 4. GoalGenerator creates goal
        goal = await generate_goal_from_parsed(
            parsed=parsed,
            goals_dir=tmp_path / "goals",
            require_approval=True,
            event_bus=event_bus,
            tenant_id="test",
        )
        assert goal.status == "pending_approval"
        assert goal.external_source is not None

        # 5. SyncManager sends progress
        channel = MockChannelAdapter()
        sync_mgr = SyncManager(channel, event_bus=event_bus)
        record = await sync_mgr.sync_goal_progress(
            goal,
            tenant_id="test",
            worker_id="worker-2",
        )
        assert record.status == "success"
