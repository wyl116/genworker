# edition: baseline
"""Tests for contact context helpers."""
from __future__ import annotations

from pathlib import Path

from src.worker.contacts.context import (
    format_contacts_markdown,
    select_contacts_for_context,
)
from src.worker.contacts.registry import ContactRegistry
from src.worker.contacts.models import PersonIdentity


def test_select_contacts_handles_none_registry():
    assert select_contacts_for_context(None) == ()


def test_format_contacts_markdown(tmp_path: Path):
    registry = ContactRegistry(tmp_path / "contacts")
    import asyncio
    asyncio.run(registry.discover_person(
        primary_name="Alice",
        identities=(PersonIdentity(channel_type="email", email="alice@example.com"),),
    ))
    contacts = select_contacts_for_context(registry, query="Alice")
    markdown = format_contacts_markdown(contacts)
    assert "Alice" in markdown
    assert "alice@example.com" in markdown
