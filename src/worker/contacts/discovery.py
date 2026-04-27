"""Contact discovery helpers."""
from __future__ import annotations

import re

from .models import PersonIdentity, PersonProfile

_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_EMPLOYEE_ID_RE = re.compile(r"\b([A-Z]{1,3}\d{2,10})\b")
_DECLARED_NAME_PATTERNS = (
    re.compile(r"我是([\u4e00-\u9fffA-Za-z0-9·]{2,30})"),
    re.compile(r"我叫([\u4e00-\u9fffA-Za-z0-9·]{2,30})"),
    re.compile(r"my name is ([A-Za-z][A-Za-z .'-]{1,40})", re.IGNORECASE),
)


class PersonExtractor:
    """Extract person candidates from emails and platform member lists."""

    def __init__(self, contact_registry=None, llm_client=None) -> None:
        self._registry = contact_registry
        self._llm_client = llm_client

    async def extract_from_email(self, email_item: dict) -> tuple[PersonProfile, ...]:
        candidates = _extract_email_candidates(email_item)
        profiles = []
        if self._registry is None:
            return tuple(candidates)
        for candidate in candidates:
            profiles.append(await self._registry.discover_person(
                primary_name=candidate.primary_name,
                identities=candidate.identities,
                role=candidate.role,
                organization=candidate.organization,
                notes=candidate.notes,
                confidence=candidate.confidence,
            ))
        return tuple(profiles)

    async def extract_from_feishu_members(self, members: list[dict]) -> tuple[PersonProfile, ...]:
        results: list[PersonProfile] = []
        for member in members:
            profile = PersonProfile(
                person_id="",
                primary_name=str(member.get("name", "")),
                identities=(PersonIdentity(
                    channel_type="feishu",
                    handle=str(member.get("open_id", member.get("user_id", ""))),
                    display_name=str(member.get("name", "")),
                    email=str(member.get("email", "")),
                    source="discovered",
                ),),
                confidence=0.6,
            )
            if self._registry is not None:
                stored = await self._registry.discover_person(
                    primary_name=profile.primary_name,
                    identities=profile.identities,
                    confidence=profile.confidence,
                )
                results.append(stored)
            else:
                results.append(profile)
        return tuple(results)

    async def extract_service_profile(
        self,
        *,
        channel_type: str,
        channel_id: str,
        message: str,
        display_name: str = "",
        topic: str = "",
    ) -> PersonProfile:
        """Create or enrich a lightweight service customer profile."""
        declared_name = _extract_declared_name(message)
        declared_identifiers = _extract_declared_identifiers(message)
        emails = tuple(dict.fromkeys(_EMAIL_RE.findall(message or "")))
        email = emails[0] if emails else ""

        if self._registry is not None:
            return await self._registry.record_service_interaction(
                channel_type=channel_type,
                channel_id=channel_id,
                message=message,
                display_name=display_name,
                declared_name=declared_name,
                declared_identifiers=declared_identifiers,
                email=email,
                topic=topic,
            )

        identities = (
            PersonIdentity(
                channel_type=channel_type,
                handle=channel_id,
                display_name=display_name or declared_name,
                email=email if channel_type == "email" else "",
                source="discovered",
            ),
        )
        return PersonProfile(
            person_id="service-candidate",
            primary_name=declared_name or display_name,
            identities=identities,
            source="discovered",
            tags=declared_identifiers,
            service_count=1,
            common_topics=(topic,) if topic else (),
            notes=(message or "").strip()[:200],
            confidence=0.6,
        )


def _extract_email_candidates(email_item: dict) -> list[PersonProfile]:
    header_values = [
        str(email_item.get("from", "")),
        str(email_item.get("to", "")),
        str(email_item.get("cc", "")),
        str(email_item.get("content", "")),
    ]
    joined = "\n".join(header_values)
    emails = list(dict.fromkeys(_EMAIL_RE.findall(joined)))
    profiles: list[PersonProfile] = []
    for index, address in enumerate(emails, 1):
        name_part = address.split("@", 1)[0].replace(".", " ").replace("_", " ").title()
        profiles.append(PersonProfile(
            person_id=f"candidate-{index}",
            primary_name=name_part,
            identities=(PersonIdentity(
                channel_type="email",
                handle=address,
                display_name=name_part,
                email=address,
                source="discovered",
            ),),
            notes=str(email_item.get("content", ""))[:200],
            confidence=0.75 if address == email_item.get("from") else 0.55,
            source="discovered",
        ))
    return profiles


def _extract_declared_name(message: str) -> str:
    text = (message or "").strip()
    for pattern in _DECLARED_NAME_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).strip(" ,，。")
    return ""


def _extract_declared_identifiers(message: str) -> tuple[str, ...]:
    text = (message or "").strip()
    identifiers = list(_EMPLOYEE_ID_RE.findall(text))
    return tuple(dict.fromkeys(identifier.strip() for identifier in identifiers if identifier.strip()))
