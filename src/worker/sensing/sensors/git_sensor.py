"""Git repository polling sensor."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from ..base import SensorBase
from ..config import RoutingRule
from ..protocol import SensedFact

DEFAULT_GIT_ROUTING_RULES: tuple[RoutingRule, ...] = (
    RoutingRule(field="branch", pattern=r"^(main|master)$", match_mode="regex", route="both"),
    RoutingRule(field="branch", pattern=r"^(release/|hotfix/)", match_mode="regex", route="reactive"),
    RoutingRule(field="is_tag", pattern="true", match_mode="equals", route="reactive"),
)


class GitSensor(SensorBase):
    """Poll git log and emit facts for unseen commits."""

    def __init__(
        self,
        *,
        repo_path: str,
        branches: tuple[str, ...] = ("main",),
        routing_rules: tuple[RoutingRule, ...] = DEFAULT_GIT_ROUTING_RULES,
        fallback_route: str = "heartbeat",
    ) -> None:
        super().__init__(routing_rules=routing_rules, fallback_route=fallback_route)
        self._repo_path = repo_path
        self._branches = branches
        self._seen_shas: set[str] = set()
        self._last_poll_iso: str = ""

    @property
    def sensor_type(self) -> str:
        return "git"

    @property
    def delivery_mode(self) -> str:
        return "poll"

    async def poll(self) -> tuple[SensedFact, ...]:
        facts: list[SensedFact] = []
        for branch in self._branches:
            for commit in await self._fetch_new_commits(branch):
                sha = str(commit.get("sha", ""))
                if not sha or sha in self._seen_shas:
                    continue
                self._seen_shas.add(sha)
                payload = (
                    ("repo", self._repo_path),
                    ("branch", branch),
                    ("sha", sha),
                    ("author", str(commit.get("author", ""))),
                    ("message", str(commit.get("message", ""))),
                    ("files_changed", ",".join(commit.get("files_changed", []))),
                    ("is_tag", str(commit.get("is_tag", False)).lower()),
                )
                route = self._classify_route(payload)
                facts.append(
                    SensedFact(
                        source_type="git",
                        event_type="external.git_commit",
                        dedupe_key=f"git:{self._repo_path}:{sha}",
                        payload=payload,
                        priority_hint=35 if route != "heartbeat" else 15,
                        cognition_route=route,
                    )
                )

        self._last_poll_iso = datetime.now(timezone.utc).isoformat()
        if len(self._seen_shas) > 5000:
            self._seen_shas = set(sorted(self._seen_shas)[-5000:])
        return tuple(facts)

    async def _fetch_new_commits(self, branch: str) -> list[dict[str, Any]]:
        stdout = await self._run_git_log(branch)
        return self._parse_git_log(stdout)

    async def _run_git_log(self, branch: str) -> str:
        cmd = [
            "git",
            "-C",
            self._repo_path,
            "log",
            "--format=%H%x1f%an%x1f%s%x1e",
            "--name-only",
            branch,
        ]
        if self._last_poll_iso:
            cmd.insert(5, f"--since={self._last_poll_iso}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode("utf-8", errors="ignore")

    def _parse_git_log(self, output: str) -> list[dict[str, Any]]:
        commits: list[dict[str, Any]] = []
        for block in output.split("\x1e"):
            block = block.strip()
            if not block:
                continue
            lines = [line for line in block.splitlines() if line.strip()]
            if not lines:
                continue
            head = lines[0].split("\x1f")
            if len(head) < 3:
                continue
            commits.append(
                {
                    "sha": head[0],
                    "author": head[1],
                    "message": head[2],
                    "files_changed": lines[1:],
                }
            )
        return commits

    def get_snapshot(self) -> dict[str, Any]:
        return {
            "seen_shas": sorted(self._seen_shas),
            "last_poll_iso": self._last_poll_iso,
        }

    def restore_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._seen_shas = set(snapshot.get("seen_shas", []))
        self._last_poll_iso = str(snapshot.get("last_poll_iso", ""))
