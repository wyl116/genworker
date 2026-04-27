"""File-backed storage for contact profiles."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

import yaml

from .models import ContactRegistryConfig, PersonIdentity, PersonProfile


class ContactStorage:
    """Persist `PersonProfile` records as frontmatter markdown files and index."""

    def __init__(self, root: Path, config: ContactRegistryConfig | None = None) -> None:
        self._root = root
        self._config = config or ContactRegistryConfig()
        self._configured_dir = root / self._config.configured_dir
        self._discovered_dir = root / self._config.discovered_dir
        self._index_path = root / self._config.index_file
        self._ensure_dirs()

    def load_all(self) -> tuple[PersonProfile, ...]:
        profiles: list[PersonProfile] = []
        for directory in (self._configured_dir, self._discovered_dir):
            for path in sorted(directory.glob("*.md")):
                profiles.append(self.load_profile(path))
        return tuple(profiles)

    def save_profile(self, profile: PersonProfile, *, configured: bool = False) -> Path:
        directory = self._configured_dir if configured else self._discovered_dir
        path = directory / f"{profile.person_id}.md"
        frontmatter = asdict(profile)
        path.write_text(
            f"---\n{yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)}---\n\n{profile.notes}\n",
            encoding="utf-8",
        )
        self._rewrite_index(self.load_all())
        return path

    def delete_profile(self, person_id: str) -> None:
        for directory in (self._configured_dir, self._discovered_dir):
            path = directory / f"{person_id}.md"
            if path.exists():
                path.unlink()
        self._rewrite_index(self.load_all())

    def load_profile(self, path: Path) -> PersonProfile:
        text = path.read_text(encoding="utf-8")
        _, raw_frontmatter, body = text.split("---", 2)
        data = yaml.safe_load(raw_frontmatter) or {}
        identities = tuple(
            PersonIdentity(**identity)
            for identity in data.get("identities", [])
        )
        return PersonProfile(
            person_id=str(data.get("person_id", path.stem)),
            primary_name=str(data.get("primary_name", "")),
            role=str(data.get("role", "")),
            organization=str(data.get("organization", "")),
            notes=str(body).strip(),
            confidence=float(data.get("confidence", 0.0) or 0.0),
            identities=identities,
            source=str(data.get("source", "configured")),
            social_circles=tuple(str(item) for item in data.get("social_circles", [])),
            is_same_org_as_owner=bool(data.get("is_same_org_as_owner", False)),
            hierarchy_level=str(data.get("hierarchy_level", "")),
            merge_history=tuple(str(item) for item in data.get("merge_history", [])),
            aliases=tuple(str(item) for item in data.get("aliases", [])),
            tags=tuple(str(item) for item in data.get("tags", [])),
            service_count=int(data.get("service_count", 0) or 0),
            common_topics=tuple(str(item) for item in data.get("common_topics", [])),
        )

    def _rewrite_index(self, profiles: Iterable[PersonProfile]) -> None:
        lines = [
            json.dumps(
                {
                    "person_id": profile.person_id,
                    "primary_name": profile.primary_name,
                    "role": profile.role,
                    "organization": profile.organization,
                    "source": profile.source,
                    "tags": list(profile.tags),
                    "service_count": profile.service_count,
                    "identities": [asdict(identity) for identity in profile.identities],
                },
                ensure_ascii=False,
            )
            for profile in profiles
        ]
        self._index_path.write_text(
            "\n".join(lines) + ("\n" if lines else ""),
            encoding="utf-8",
        )

    def _ensure_dirs(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._configured_dir.mkdir(parents=True, exist_ok=True)
        self._discovered_dir.mkdir(parents=True, exist_ok=True)
        if not self._index_path.exists():
            self._index_path.write_text("", encoding="utf-8")
