"""Contact registry CRUD, search, merge and discovery helpers."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from src.events.models import Event

from .models import ContactRegistryConfig, PersonIdentity, PersonProfile
from .storage import ContactStorage


class ContactRegistry:
    """File-backed registry of known contacts for one worker."""

    def __init__(
        self,
        root: Path,
        *,
        event_bus=None,
        config: ContactRegistryConfig | None = None,
    ) -> None:
        self._storage = ContactStorage(root, config)
        self._event_bus = event_bus
        self._profiles: dict[str, PersonProfile] = {
            profile.person_id: profile for profile in self._storage.load_all()
        }

    def list_contacts(self) -> tuple[PersonProfile, ...]:
        return tuple(self._profiles.values())

    def get(self, person_id: str) -> PersonProfile | None:
        return self._profiles.get(person_id)

    async def discover_person(
        self,
        *,
        primary_name: str,
        identities: tuple[PersonIdentity, ...] = (),
        role: str = "",
        organization: str = "",
        notes: str = "",
        confidence: float = 0.5,
        source: str = "discovered",
        tags: tuple[str, ...] = (),
        common_topics: tuple[str, ...] = (),
        service_count_increment: int = 0,
    ) -> PersonProfile:
        existing = (
            self._find_by_identity(identities)
            or self._find_by_tags(tags)
            or self._find_by_name(primary_name)
        )
        if existing is None:
            profile = PersonProfile(
                person_id=f"person-{uuid4().hex[:12]}",
                primary_name=primary_name,
                role=role,
                organization=organization,
                notes=notes,
                confidence=confidence,
                identities=identities,
                source=source,
                tags=_merge_values((), tags),
                common_topics=_merge_values((), common_topics),
                service_count=max(service_count_increment, 0),
            )
        else:
            merged_identities = _merge_identities(existing.identities, identities)
            profile = replace(
                existing,
                primary_name=primary_name or existing.primary_name,
                role=role or existing.role,
                organization=organization or existing.organization,
                notes=notes or existing.notes,
                confidence=max(existing.confidence, confidence),
                identities=merged_identities,
                source=_merge_source(existing.source, source),
                tags=_merge_values(existing.tags, tags),
                common_topics=_merge_values(existing.common_topics, common_topics),
                service_count=existing.service_count + max(service_count_increment, 0),
            )
        self._profiles[profile.person_id] = profile
        self._storage.save_profile(profile)
        if self._event_bus is not None:
            await self._event_bus.publish(Event(
                event_id=f"evt-contact-{profile.person_id}",
                type="contact.discovered",
                source="contact_registry",
                tenant_id="demo",
                payload=(("person_id", profile.person_id), ("name", profile.primary_name)),
            ))
        return profile

    async def record_service_interaction(
        self,
        *,
        channel_type: str,
        channel_id: str,
        message: str = "",
        display_name: str = "",
        declared_name: str = "",
        declared_identifiers: tuple[str, ...] = (),
        email: str = "",
        topic: str = "",
        confidence: float = 0.6,
    ) -> PersonProfile:
        """Create or enrich a service customer profile from one interaction."""
        normalized_tags = tuple(
            tag.strip() for tag in declared_identifiers if str(tag).strip()
        )
        identities = _build_service_identities(
            channel_type=channel_type,
            channel_id=channel_id,
            display_name=display_name or declared_name,
            email=email,
        )
        name = declared_name.strip() or display_name.strip()
        notes = (message or "").strip()[:200]
        topics = (topic.strip(),) if topic.strip() else ()
        return await self.discover_person(
            primary_name=name,
            identities=identities,
            notes=notes,
            confidence=confidence,
            source="discovered",
            tags=normalized_tags,
            common_topics=topics,
            service_count_increment=1,
        )

    def search_contacts(
        self,
        *,
        query: str = "",
        channel_type: str = "",
        role: str = "",
    ) -> tuple[PersonProfile, ...]:
        query_lower = query.lower().strip()
        results: list[tuple[int, PersonProfile]] = []
        for profile in self._profiles.values():
            if channel_type and not any(
                identity.channel_type == channel_type for identity in profile.identities
            ):
                continue
            if role and role.lower() not in profile.role.lower():
                continue
            score = 0
            if query_lower:
                if query_lower in profile.primary_name.lower():
                    score += 3
                if any(query_lower in alias.lower() for alias in profile.aliases):
                    score += 2
                if any(query_lower in tag.lower() for tag in profile.tags):
                    score += 2
                if any(query_lower in identity.handle.lower() for identity in profile.identities):
                    score += 1
            results.append((score, profile))
        results.sort(key=lambda item: (item[0], item[1].confidence), reverse=True)
        return tuple(profile for _, profile in results if query_lower == "" or _ > 0)

    def suggest_merge(self, left_id: str, right_id: str) -> dict[str, object]:
        left = self._profiles[left_id]
        right = self._profiles[right_id]
        shared_email = any(
            l.email and l.email == r.email
            for l in left.identities
            for r in right.identities
        )
        score = 0.95 if shared_email else _name_similarity(left.primary_name, right.primary_name)
        return {"left_id": left_id, "right_id": right_id, "confidence": score}

    def confirm_merge(self, left_id: str, right_id: str) -> PersonProfile:
        left = self._profiles[left_id]
        right = self._profiles[right_id]
        merged = PersonProfile(
            person_id=left.person_id,
            primary_name=left.primary_name or right.primary_name,
            role=left.role or right.role,
            organization=left.organization or right.organization,
            notes=left.notes or right.notes,
            confidence=max(left.confidence, right.confidence),
            identities=_merge_identities(left.identities, right.identities),
            source=_merge_source(left.source, right.source),
            social_circles=tuple(dict.fromkeys((*left.social_circles, *right.social_circles))),
            is_same_org_as_owner=left.is_same_org_as_owner or right.is_same_org_as_owner,
            hierarchy_level=left.hierarchy_level or right.hierarchy_level,
            merge_history=(*left.merge_history, right.person_id, *right.merge_history),
            aliases=tuple(dict.fromkeys((*left.aliases, right.primary_name, *right.aliases))),
            tags=_merge_values(left.tags, right.tags),
            service_count=left.service_count + right.service_count,
            common_topics=_merge_values(left.common_topics, right.common_topics),
        )
        self._profiles[left_id] = merged
        self._storage.save_profile(merged)
        self._profiles.pop(right_id, None)
        self._storage.delete_profile(right_id)
        return merged

    def unmerge(self, person_id: str, restored_profile: PersonProfile) -> None:
        self._profiles[restored_profile.person_id] = restored_profile
        current = self._profiles[person_id]
        history = tuple(item for item in current.merge_history if item != restored_profile.person_id)
        self._profiles[person_id] = replace(current, merge_history=history)
        self._storage.save_profile(self._profiles[person_id])
        self._storage.save_profile(restored_profile)

    def bootstrap_configured(self, contacts: tuple[PersonProfile, ...]) -> None:
        for profile in contacts:
            self._profiles[profile.person_id] = profile
            self._storage.save_profile(profile, configured=True)

    def _find_by_identity(self, identities: tuple[PersonIdentity, ...]) -> PersonProfile | None:
        candidates = {
            (identity.channel_type, identity.handle, identity.email)
            for identity in identities
        }
        for profile in self._profiles.values():
            for identity in profile.identities:
                if (identity.channel_type, identity.handle, identity.email) in candidates:
                    return profile
        return None

    def _find_by_name(self, primary_name: str) -> PersonProfile | None:
        wanted = primary_name.strip().lower()
        if not wanted:
            return None
        for profile in self._profiles.values():
            if profile.primary_name.lower() == wanted:
                return profile
        return None

    def _find_by_tags(self, tags: tuple[str, ...]) -> PersonProfile | None:
        wanted = {tag.strip().lower() for tag in tags if tag.strip()}
        if not wanted:
            return None
        for profile in self._profiles.values():
            profile_tags = {tag.lower() for tag in profile.tags}
            if wanted & profile_tags:
                return profile
        return None


def _merge_identities(
    left: tuple[PersonIdentity, ...],
    right: tuple[PersonIdentity, ...],
) -> tuple[PersonIdentity, ...]:
    seen: set[tuple[str, str, str]] = set()
    merged: list[PersonIdentity] = []
    for identity in (*left, *right):
        key = (identity.channel_type, identity.handle, identity.email)
        if key in seen:
            continue
        seen.add(key)
        merged.append(identity)
    return tuple(merged)


def _name_similarity(left: str, right: str) -> float:
    left_tokens = set(left.lower().split())
    right_tokens = set(right.lower().split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _merge_values(
    left: tuple[str, ...],
    right: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*left, *right)))


def _merge_source(left: str, right: str) -> str:
    if left == "configured" or right == "configured":
        return "configured"
    if left == "merged" or right == "merged":
        return "merged"
    return right or left or "discovered"


def _build_service_identities(
    *,
    channel_type: str,
    channel_id: str,
    display_name: str,
    email: str,
) -> tuple[PersonIdentity, ...]:
    identities: list[PersonIdentity] = [
        PersonIdentity(
            channel_type=channel_type,
            handle=channel_id,
            display_name=display_name,
            email=email if channel_type == "email" else "",
            source="discovered",
        )
    ]
    if email and channel_type != "email":
        identities.append(PersonIdentity(
            channel_type="email",
            handle=email,
            display_name=display_name,
            email=email,
            source="discovered",
        ))
    return tuple(identities)
