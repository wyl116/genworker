"""Helpers for selecting and formatting contact context."""
from __future__ import annotations

from .models import PersonProfile


def select_contacts_for_context(
    registry,
    *,
    query: str = "",
    limit: int = 8,
) -> tuple[PersonProfile, ...]:
    if registry is None:
        return ()
    contacts = registry.search_contacts(query=query) if query else registry.list_contacts()
    return tuple(contacts[:limit])


def format_contacts_markdown(contacts: tuple[PersonProfile, ...]) -> str:
    if not contacts:
        return ""
    lines = ["[Contact Context]"]
    for contact in contacts:
        identifiers = ", ".join(
            filter(None, [identity.email or identity.handle for identity in contact.identities])
        )
        lines.append(
            f"- {contact.primary_name}"
            f"{f' ({contact.role})' if contact.role else ''}"
            f"{f' [{identifiers}]' if identifiers else ''}"
        )
    return "\n".join(lines)
