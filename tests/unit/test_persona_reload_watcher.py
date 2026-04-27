# edition: baseline
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.core.persona_reload_watcher import PersonaReloadWatcher


def _write_persona(path: Path, name: str = "worker") -> None:
    path.write_text(
        f"---\nidentity:\n  worker_id: w1\n  name: {name}\n---\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_scan_once_seeds_without_reloading(tmp_path: Path):
    worker_dir = tmp_path / "tenants" / "demo" / "workers" / "w1"
    worker_dir.mkdir(parents=True, exist_ok=True)
    persona = worker_dir / "PERSONA.md"
    _write_persona(persona)

    calls = []

    async def _reload(worker_id: str, tenant_id: str):
        calls.append((tenant_id, worker_id))
        return {"tenant_id": tenant_id, "worker_id": worker_id}

    watcher = PersonaReloadWatcher(workspace_root=tmp_path, reload_worker=_reload)
    changed = await watcher.scan_once()

    assert changed == []
    assert calls == []


@pytest.mark.asyncio
async def test_scan_once_reloads_changed_persona(tmp_path: Path):
    worker_dir = tmp_path / "tenants" / "demo" / "workers" / "w1"
    worker_dir.mkdir(parents=True, exist_ok=True)
    persona = worker_dir / "PERSONA.md"
    _write_persona(persona)

    calls = []

    async def _reload(worker_id: str, tenant_id: str):
        calls.append((tenant_id, worker_id))
        return {"tenant_id": tenant_id, "worker_id": worker_id}

    watcher = PersonaReloadWatcher(
        workspace_root=tmp_path,
        reload_worker=_reload,
        debounce_seconds=0.01,
    )
    await watcher.scan_once()
    await asyncio.sleep(0.02)
    _write_persona(persona, name="updated")

    changed = await watcher.scan_once()

    assert calls == [("demo", "w1")]
    assert changed == [{"tenant_id": "demo", "worker_id": "w1"}]


@pytest.mark.asyncio
async def test_scan_once_debounces_multiple_changes(tmp_path: Path):
    worker_dir = tmp_path / "tenants" / "demo" / "workers" / "w1"
    worker_dir.mkdir(parents=True, exist_ok=True)
    persona = worker_dir / "PERSONA.md"
    _write_persona(persona)

    calls = []

    async def _reload(worker_id: str, tenant_id: str):
        calls.append((tenant_id, worker_id))
        return {"tenant_id": tenant_id, "worker_id": worker_id}

    watcher = PersonaReloadWatcher(
        workspace_root=tmp_path,
        reload_worker=_reload,
        debounce_seconds=10.0,
    )
    await watcher.scan_once()
    _write_persona(persona, name="a")
    await watcher.scan_once()
    _write_persona(persona, name="b")
    await watcher.scan_once()

    assert calls == [("demo", "w1")]


@pytest.mark.asyncio
async def test_scan_once_reloads_when_duty_file_is_added(tmp_path: Path):
    worker_dir = tmp_path / "tenants" / "demo" / "workers" / "w1"
    (worker_dir / "duties").mkdir(parents=True, exist_ok=True)
    _write_persona(worker_dir / "PERSONA.md")

    calls = []

    async def _reload(worker_id: str, tenant_id: str):
        calls.append((tenant_id, worker_id))
        return {"tenant_id": tenant_id, "worker_id": worker_id}

    watcher = PersonaReloadWatcher(
        workspace_root=tmp_path,
        reload_worker=_reload,
        debounce_seconds=0.01,
    )
    await watcher.scan_once()
    await asyncio.sleep(0.02)

    (worker_dir / "duties" / "new-duty.md").write_text(
        (
            "---\n"
            "duty_id: new-duty\n"
            "title: New Duty\n"
            "status: active\n"
            "triggers:\n"
            "  - id: morning\n"
            "    type: schedule\n"
            "    cron: \"0 9 * * *\"\n"
            "quality_criteria:\n"
            "  - complete\n"
            "---\n"
            "Run the new duty.\n"
        ),
        encoding="utf-8",
    )

    changed = await watcher.scan_once()

    assert calls == [("demo", "w1")]
    assert changed == [{"tenant_id": "demo", "worker_id": "w1"}]
    assert watcher.operational_snapshot["tracked_files"] == 2


@pytest.mark.asyncio
async def test_scan_once_reloads_when_goal_file_is_deleted(tmp_path: Path):
    worker_dir = tmp_path / "tenants" / "demo" / "workers" / "w1"
    (worker_dir / "goals").mkdir(parents=True, exist_ok=True)
    _write_persona(worker_dir / "PERSONA.md")
    goal_file = worker_dir / "goals" / "goal-1.md"
    goal_file.write_text(
        (
            "---\n"
            "goal_id: goal-1\n"
            "title: Test Goal\n"
            "status: active\n"
            "priority: high\n"
            "milestones:\n"
            "  - id: ms-1\n"
            "    title: First milestone\n"
            "    status: pending\n"
            "    tasks:\n"
            "      - id: task-1\n"
            "        title: Investigate\n"
            "        status: pending\n"
            "---\n"
            "Goal body.\n"
        ),
        encoding="utf-8",
    )

    calls = []

    async def _reload(worker_id: str, tenant_id: str):
        calls.append((tenant_id, worker_id))
        return {"tenant_id": tenant_id, "worker_id": worker_id}

    watcher = PersonaReloadWatcher(
        workspace_root=tmp_path,
        reload_worker=_reload,
        debounce_seconds=0.01,
    )
    await watcher.scan_once()
    await asyncio.sleep(0.02)
    goal_file.unlink()

    changed = await watcher.scan_once()

    assert calls == [("demo", "w1")]
    assert changed == [{"tenant_id": "demo", "worker_id": "w1"}]
    assert watcher.operational_snapshot["tracked_files"] == 1


@pytest.mark.asyncio
async def test_scan_once_reloads_when_rule_file_changes(tmp_path: Path):
    worker_dir = tmp_path / "tenants" / "demo" / "workers" / "w1"
    rules_dir = worker_dir / "rules" / "directives"
    rules_dir.mkdir(parents=True, exist_ok=True)
    _write_persona(worker_dir / "PERSONA.md")
    rule_file = rules_dir / "dir-1.md"
    rule_file.write_text(
        (
            "---\n"
            "rule_id: dir-1\n"
            "type: directive\n"
            "category: policy\n"
            "status: active\n"
            "confidence: 1.0\n"
            "source:\n"
            "  type: admin\n"
            "  evidence: configured\n"
            "  created_at: 2026-04-07T00:00:00Z\n"
            "---\n"
            "# Always answer in Chinese\n\n"
            "Admin directive.\n"
        ),
        encoding="utf-8",
    )

    calls = []

    async def _reload(worker_id: str, tenant_id: str):
        calls.append((tenant_id, worker_id))
        return {"tenant_id": tenant_id, "worker_id": worker_id}

    watcher = PersonaReloadWatcher(
        workspace_root=tmp_path,
        reload_worker=_reload,
        debounce_seconds=0.01,
    )
    await watcher.scan_once()
    await asyncio.sleep(0.02)
    rule_file.write_text(
        (
            "---\n"
            "rule_id: dir-1\n"
            "type: directive\n"
            "category: policy\n"
            "status: active\n"
            "confidence: 1.0\n"
            "source:\n"
            "  type: admin\n"
            "  evidence: configured\n"
            "  created_at: 2026-04-07T00:00:00Z\n"
            "---\n"
            "# Always answer in Chinese and concise\n\n"
            "Updated admin directive.\n"
        ),
        encoding="utf-8",
    )

    changed = await watcher.scan_once()

    assert calls == [("demo", "w1")]
    assert changed == [{"tenant_id": "demo", "worker_id": "w1"}]
    assert watcher.operational_snapshot["tracked_files"] == 2


@pytest.mark.asyncio
async def test_scan_once_reloads_when_skill_file_changes(tmp_path: Path):
    worker_dir = tmp_path / "tenants" / "demo" / "workers" / "w1"
    skill_dir = worker_dir / "skills" / "crystallized-test"
    skill_dir.mkdir(parents=True, exist_ok=True)
    _write_persona(worker_dir / "PERSONA.md")
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: test\nmetadata:\n  genworker:\n    scope: worker\n---\n## instructions.general\nDo it.\n",
        encoding="utf-8",
    )

    calls = []

    async def _reload(worker_id: str, tenant_id: str):
        calls.append((tenant_id, worker_id))
        return {"tenant_id": tenant_id, "worker_id": worker_id}

    watcher = PersonaReloadWatcher(
        workspace_root=tmp_path,
        reload_worker=_reload,
        debounce_seconds=0.01,
    )
    await watcher.scan_once()
    await asyncio.sleep(0.02)
    skill_file.write_text(
        "---\nname: test\nmetadata:\n  genworker:\n    scope: worker\n---\n## instructions.general\nUpdated.\n",
        encoding="utf-8",
    )

    changed = await watcher.scan_once()
    assert calls == [("demo", "w1")]
    assert changed == [{"tenant_id": "demo", "worker_id": "w1"}]
