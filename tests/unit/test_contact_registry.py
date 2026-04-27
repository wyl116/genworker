# edition: baseline
"""Tests for ContactRegistry."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.worker.contacts import ContactRegistry
from src.worker.contacts.models import PersonIdentity, PersonProfile


@pytest.mark.asyncio
async def test_discover_person_persists_and_searches(tmp_path: Path):
    registry = ContactRegistry(tmp_path / "contacts")
    profile = await registry.discover_person(
        primary_name="Alice Chen",
        identities=(PersonIdentity(channel_type="email", email="alice@example.com", handle="alice@example.com"),),
        role="PM",
        confidence=0.8,
    )

    results = registry.search_contacts(query="Alice")

    assert profile.person_id
    assert results[0].primary_name == "Alice Chen"


def test_confirm_merge_keeps_history_and_unmerge(tmp_path: Path):
    registry = ContactRegistry(tmp_path / "contacts")
    left = PersonProfile(person_id="p1", primary_name="Alice", aliases=("A",))
    right = PersonProfile(person_id="p2", primary_name="Alice Chen")
    registry.bootstrap_configured((left, right))

    merged = registry.confirm_merge("p1", "p2")
    assert "p2" in merged.merge_history

    registry.unmerge("p1", right)
    assert registry.get("p2") is not None


@pytest.mark.asyncio
async def test_record_service_interaction_creates_light_customer_profile(tmp_path: Path):
    registry = ContactRegistry(tmp_path / "contacts")

    profile = await registry.record_service_interaction(
        channel_type="feishu",
        channel_id="ou_xxx",
        message="请问退款流程是什么？",
        topic="refund",
    )

    assert profile.source == "discovered"
    assert profile.service_count == 1
    assert profile.common_topics == ("refund",)
    assert profile.identities[0].channel_type == "feishu"
    assert profile.identities[0].handle == "ou_xxx"


@pytest.mark.asyncio
async def test_record_service_interaction_enriches_existing_profile_by_channel(tmp_path: Path):
    registry = ContactRegistry(tmp_path / "contacts")

    first = await registry.record_service_interaction(
        channel_type="feishu",
        channel_id="ou_xxx",
        message="请问退款流程是什么？",
    )
    second = await registry.record_service_interaction(
        channel_type="feishu",
        channel_id="ou_xxx",
        message="我是张三，工号 E001，上次的退款还没到账",
        declared_name="张三",
        declared_identifiers=("E001",),
        topic="refund",
    )

    assert second.person_id == first.person_id
    assert second.primary_name == "张三"
    assert "E001" in second.tags
    assert second.service_count == 2


@pytest.mark.asyncio
async def test_record_service_interaction_merges_by_identifier_across_channels(tmp_path: Path):
    registry = ContactRegistry(tmp_path / "contacts")

    first = await registry.record_service_interaction(
        channel_type="feishu",
        channel_id="ou_xxx",
        message="我是张三，工号 E001",
        declared_name="张三",
        declared_identifiers=("E001",),
    )
    second = await registry.record_service_interaction(
        channel_type="email",
        channel_id="zhangsan@company.com",
        message="我是张三 E001，退款问题",
        declared_name="张三",
        declared_identifiers=("E001",),
        email="zhangsan@company.com",
        topic="refund",
    )

    assert second.person_id == first.person_id
    assert second.service_count == 2
    assert any(identity.channel_type == "email" for identity in second.identities)
