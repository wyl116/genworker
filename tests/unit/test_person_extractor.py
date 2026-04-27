# edition: baseline
"""Tests for PersonExtractor and relationship inference."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.worker.contacts.discovery import PersonExtractor
from src.worker.contacts.inference import IdentityResolver, RelationshipInferrer
from src.worker.contacts.models import PersonIdentity, PersonProfile
from src.worker.contacts.registry import ContactRegistry


@pytest.mark.asyncio
async def test_extract_from_email_without_llm(tmp_path: Path):
    registry = ContactRegistry(tmp_path / "contacts")
    extractor = PersonExtractor(registry, llm_client=None)

    profiles = await extractor.extract_from_email({
        "from": "alice@example.com",
        "content": "Hi,\nAlice Chen\nalice@example.com",
    })

    assert profiles[0].primary_name == "Alice"


@pytest.mark.asyncio
async def test_extract_service_profile_progressively_enriches_customer(tmp_path: Path):
    registry = ContactRegistry(tmp_path / "contacts")
    extractor = PersonExtractor(registry, llm_client=None)

    first = await extractor.extract_service_profile(
        channel_type="feishu",
        channel_id="ou_xxx",
        message="请问退款流程是什么？",
        topic="refund",
    )
    second = await extractor.extract_service_profile(
        channel_type="feishu",
        channel_id="ou_xxx",
        message="我是张三，工号 E001，上次的退款还没到账",
        topic="refund",
    )
    third = await extractor.extract_service_profile(
        channel_type="email",
        channel_id="zhangsan@company.com",
        message="我是张三 E001，退款问题",
        topic="refund",
    )

    assert first.service_count == 1
    assert second.person_id == first.person_id
    assert second.primary_name == "张三"
    assert "E001" in second.tags
    assert third.person_id == first.person_id
    assert third.service_count == 3
    assert any(identity.channel_type == "email" for identity in third.identities)


def test_identity_resolver_matches_email():
    resolver = IdentityResolver()
    existing = PersonProfile(
        person_id="p1",
        primary_name="Alice Chen",
        identities=(PersonIdentity(channel_type="email", email="alice@example.com"),),
    )
    candidate = PersonProfile(
        person_id="p2",
        primary_name="Alice",
        identities=(PersonIdentity(channel_type="email", email="alice@example.com"),),
    )

    result = resolver.resolve(candidate, (existing,))

    assert result["person_id"] == "p1"


def test_relationship_inferrer_marks_same_org():
    inferrer = RelationshipInferrer()
    profile = PersonProfile(
        person_id="p1",
        primary_name="Alice",
        identities=(PersonIdentity(channel_type="email", email="alice@company.com"),),
    )

    inferred = inferrer.infer(profile, owner_domain="company.com")

    assert inferred.is_same_org_as_owner is True
    assert "colleague" in inferred.social_circles
