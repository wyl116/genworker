"""Identity merge and relationship inference helpers."""
from __future__ import annotations

from dataclasses import replace

from .models import PersonProfile


class IdentityResolver:
    """Resolve probable duplicate contacts."""

    def resolve(
        self,
        candidate: PersonProfile,
        contacts: tuple[PersonProfile, ...],
    ) -> dict[str, object] | None:
        best: tuple[float, PersonProfile] | None = None
        for profile in contacts:
            score = _match_score(candidate, profile)
            if best is None or score > best[0]:
                best = (score, profile)
        if best is None or best[0] < 0.6:
            return None
        return {"person_id": best[1].person_id, "confidence": best[0]}


class RelationshipInferrer:
    """Infer organizational relationship hints from profile fields."""

    def infer(self, profile: PersonProfile, owner_domain: str = "") -> PersonProfile:
        identities = [identity.email for identity in profile.identities if identity.email]
        same_org = bool(owner_domain) and any(
            email.lower().endswith(f"@{owner_domain.lower()}") for email in identities
        )
        social_circles = profile.social_circles
        if same_org and "colleague" not in social_circles:
            social_circles = (*social_circles, "colleague")
        return replace(
            profile,
            is_same_org_as_owner=same_org or profile.is_same_org_as_owner,
            social_circles=social_circles,
            hierarchy_level=profile.hierarchy_level or ("peer" if same_org else ""),
        )


def _match_score(left: PersonProfile, right: PersonProfile) -> float:
    left_emails = {identity.email for identity in left.identities if identity.email}
    right_emails = {identity.email for identity in right.identities if identity.email}
    if left_emails & right_emails:
        return 0.95
    left_name = set(left.primary_name.lower().split())
    right_name = set(right.primary_name.lower().split())
    if not left_name or not right_name:
        return 0.0
    return len(left_name & right_name) / len(left_name | right_name)
