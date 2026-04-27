# edition: baseline
from src.bootstrap.context import BootstrapContext
from src.runtime.default_worker_resolver import resolve_default_worker
from src.skills.registry import SkillRegistry
from src.worker.models import Worker, WorkerIdentity, WorkerMode
from src.worker.registry import WorkerEntry, build_worker_registry


def _entry(worker_id: str, *, mode: WorkerMode = WorkerMode.PERSONAL) -> WorkerEntry:
    return WorkerEntry(
        worker=Worker(
            identity=WorkerIdentity(name=worker_id, worker_id=worker_id),
            mode=mode,
        ),
        skill_registry=SkillRegistry(),
    )


def test_resolve_default_worker_prefers_explicit_default():
    context = BootstrapContext()
    context.set_state(
        "worker_registry",
        build_worker_registry([_entry("w1"), _entry("w2")], default_worker_id="w2"),
    )

    selection = resolve_default_worker(context)

    assert selection.worker_id == "w2"
    assert selection.worker_loaded is True


def test_resolve_default_worker_prefers_demo_worker_when_present():
    context = BootstrapContext()
    context.set_state("tenant_id", "demo")
    context.set_state(
        "worker_registry",
        build_worker_registry([_entry("analyst-01"), _entry("worker-2")]),
    )

    selection = resolve_default_worker(context)

    assert selection.worker_id == "analyst-01"
    assert selection.worker_loaded is True


def test_resolve_default_worker_skips_service_workers_for_fallback():
    context = BootstrapContext()
    context.set_state(
        "worker_registry",
        build_worker_registry([
            _entry("svc-1", mode=WorkerMode.SERVICE),
            _entry("worker-2"),
        ]),
    )

    selection = resolve_default_worker(context)

    assert selection.worker_id == "worker-2"
    assert selection.worker_loaded is True


def test_resolve_default_worker_returns_empty_when_no_chat_worker():
    context = BootstrapContext()
    context.set_state(
        "worker_registry",
        build_worker_registry([_entry("svc-1", mode=WorkerMode.SERVICE)]),
    )

    selection = resolve_default_worker(context)

    assert selection.worker_id == ""
    assert selection.worker_loaded is False
