# edition: baseline
from dataclasses import replace

from src.worker.duty.execution_log import write_execution_record
from src.worker.duty.models import DutyExecutionRecord
from src.worker.lifecycle.duty_builder import build_duty_from_payload, write_duty_md
from src.worker.lifecycle.duty_skill_detector import DutySkillDetector
from src.worker.lifecycle.detectors import DutyDriftDetector, RepeatedTaskDetector
from src.worker.lifecycle.feedback_store import FeedbackStore
from src.worker.lifecycle.models import FeedbackRecord, SuggestionRecord, add_days_iso, now_iso
from src.worker.lifecycle.suggestion_store import SuggestionStore
from src.worker.task import TaskStore, create_task_manifest


def _make_duty_skill_detector(tmp_path, *, duty_id="duty-report-1", skill_id=None):
    suggestion_store = SuggestionStore(tmp_path)
    detector = DutySkillDetector(suggestion_store=suggestion_store)
    duties_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "duties"
    duty = build_duty_from_payload(
        {
            "duty_id": duty_id,
            "title": "每周周报汇总",
            "schedule": "0 9 * * 1",
            "action": "收集销售数据并输出周报摘要",
            "quality_criteria": ["完整", "准确"],
        }
    )
    if skill_id is not None:
        duty = replace(duty, skill_id=skill_id)
    write_duty_md(duty, duties_dir, filename=f"{duty_id}.md")
    return suggestion_store, detector, duties_dir, duty


def _write_duty_runs(
    duties_dir,
    duty_id: str,
    *,
    total: int,
    anomalies_every: int | None = None,
    escalated_every: int | None = None,
    failure_every: int | None = None,
    conclusion: str = "completed successfully",
):
    duty_log_dir = duties_dir / duty_id
    for index in range(total):
        anomalies = ("issue",) if anomalies_every and (index + 1) % anomalies_every == 0 else ()
        escalated = bool(escalated_every and (index + 1) % escalated_every == 0)
        final_conclusion = "error while processing" if failure_every and (index + 1) % failure_every == 0 else conclusion
        write_execution_record(
            duty_log_dir,
            DutyExecutionRecord(
                execution_id=f"{duty_id}-{index}",
                duty_id=duty_id,
                trigger_id="schedule-1",
                depth="standard",
                executed_at=f"2026-04-{index + 1:02d}T09:00:00+00:00",
                duration_seconds=3.0,
                conclusion=final_conclusion,
                anomalies_found=anomalies,
                escalated=escalated,
            ),
        )


def test_repeated_task_detector_creates_task_to_duty_suggestion(tmp_path):
    task_store = TaskStore(tmp_path)
    suggestion_store = SuggestionStore(tmp_path)
    detector = RepeatedTaskDetector(task_store=task_store, suggestion_store=suggestion_store)

    for index in range(5):
        manifest = create_task_manifest(
            worker_id="worker-1",
            tenant_id="tenant-1",
            task_description="检查上周客户反馈汇总",
        ).mark_completed("done")
        task_store.save(
            replace(
                manifest,
                created_at=add_days_iso(now_iso(), -(index + 1)),
            )
        )

    created = detector.detect(tenant_id="tenant-1", worker_id="worker-1")

    assert len(created) == 1
    assert created[0].type == "task_to_duty"
    assert "schedule" in created[0].payload_dict
    assert created[0].payload_dict["duty_id"].startswith("duty-")
    assert created[0].payload_dict["duty_id"].isascii()


def test_duty_drift_detector_creates_redefine_suggestion(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    detector = DutyDriftDetector(
        suggestion_store=suggestion_store,
        feedback_store=feedback_store,
    )
    duties_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "duties"
    duty = build_duty_from_payload(
        {
            "duty_id": "duty-1",
            "title": "日报整理",
            "schedule": "0 9 * * *",
            "action": "整理日报内容",
            "quality_criteria": ["完整", "准确"],
        }
    )
    write_duty_md(duty, duties_dir, filename="legacy-title.md")
    duty_log_dir = duties_dir / duty.duty_id
    for index in range(3):
        write_execution_record(
            duty_log_dir,
            DutyExecutionRecord(
                execution_id=f"exec-{index}",
                duty_id="duty-1",
                trigger_id="cron-1",
                depth="standard",
                executed_at=f"2026-04-0{index + 1}T09:00:00+00:00",
                duration_seconds=1.0,
                conclusion="bad result",
                escalated=False,
            ),
        )
        feedback_store.append(
            "tenant-1",
            "worker-1",
            FeedbackRecord(
                feedback_id=f"fb-{index}",
                target_type="duty",
                target_id="duty-1",
                verdict="rejected",
                reason="结果不对",
                created_by="user:test",
            ),
        )

    created = detector.detect(
        tenant_id="tenant-1",
        worker_id="worker-1",
        duties_dir=duties_dir,
    )

    assert len(created) == 1
    assert created[0].type == "duty_redefine"
    assert created[0].source_entity_id == "duty-1"
    assert created[0].payload_dict["recommended_action"] in {
        "redefine_action",
        "pause",
        "tighten_quality_criteria",
    }


def test_repeated_task_detector_skips_cluster_with_approved_suggestion(tmp_path):
    task_store = TaskStore(tmp_path)
    suggestion_store = SuggestionStore(tmp_path)
    detector = RepeatedTaskDetector(task_store=task_store, suggestion_store=suggestion_store)

    for index in range(5):
        manifest = create_task_manifest(
            worker_id="worker-1",
            tenant_id="tenant-1",
            task_description="检查上周客户反馈汇总",
        ).mark_completed("done")
        task_store.save(
            replace(
                manifest,
                created_at=add_days_iso(now_iso(), -(index + 1)),
            )
        )

    cluster_key = "检查{period}客户反馈汇总"
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-task-1",
            type="task_to_duty",
            source_entity_type="task_cluster",
            source_entity_id=cluster_key,
            title="cluster",
            reason="repeat",
            expires_at=add_days_iso(now_iso(), 30),
        ),
    )
    suggestion_store.resolve(
        "tenant-1",
        "worker-1",
        "sugg-task-1",
        status="approved",
        resolved_by="user:test",
    )

    created = detector.detect(tenant_id="tenant-1", worker_id="worker-1")

    assert created == ()


def test_duty_skill_detector_creates_duty_to_skill_suggestion(tmp_path):
    suggestion_store, detector, duties_dir, duty = _make_duty_skill_detector(tmp_path)
    _write_duty_runs(duties_dir, duty.duty_id, total=10)

    created = detector.detect(
        tenant_id="tenant-1",
        worker_id="worker-1",
        duties_dir=duties_dir,
    )

    assert len(created) == 1
    assert created[0].type == "duty_to_skill"
    assert created[0].payload_dict["source_duty_id"] == "duty-report-1"
    assert created[0].payload_dict["skill_id"].isascii()


def test_duty_skill_detector_skips_bound_or_failing_duties(tmp_path):
    suggestion_store = SuggestionStore(tmp_path)
    detector = DutySkillDetector(suggestion_store=suggestion_store)
    duties_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "duties"
    _, _, _, bound_duty = _make_duty_skill_detector(
        tmp_path,
        duty_id="duty-bound-1",
        skill_id="skill-existing-1",
    )
    failing_duty = build_duty_from_payload(
        {
            "duty_id": "duty-failing-1",
            "title": "持续失败任务",
            "schedule": "0 10 * * 1",
            "action": "执行任务",
            "quality_criteria": ["完整"],
        }
    )
    write_duty_md(failing_duty, duties_dir, filename="duty-failing-1.md")
    _write_duty_runs(duties_dir, bound_duty.duty_id, total=10, conclusion="completed")
    _write_duty_runs(duties_dir, failing_duty.duty_id, total=10, failure_every=1)

    created = detector.detect(
        tenant_id="tenant-1",
        worker_id="worker-1",
        duties_dir=duties_dir,
    )

    assert created == ()


def test_duty_skill_detector_skips_high_anomaly_rate(tmp_path):
    _, detector, duties_dir, duty = _make_duty_skill_detector(tmp_path)
    _write_duty_runs(duties_dir, duty.duty_id, total=20, anomalies_every=4)

    created = detector.detect(tenant_id="tenant-1", worker_id="worker-1", duties_dir=duties_dir)

    assert created == ()


def test_duty_skill_detector_skips_high_escalation_rate(tmp_path):
    _, detector, duties_dir, duty = _make_duty_skill_detector(tmp_path)
    _write_duty_runs(duties_dir, duty.duty_id, total=20, escalated_every=4)

    created = detector.detect(tenant_id="tenant-1", worker_id="worker-1", duties_dir=duties_dir)

    assert created == ()


def test_duty_skill_detector_skips_insufficient_executions(tmp_path):
    _, detector, duties_dir, duty = _make_duty_skill_detector(tmp_path)
    _write_duty_runs(duties_dir, duty.duty_id, total=5)

    created = detector.detect(tenant_id="tenant-1", worker_id="worker-1", duties_dir=duties_dir)

    assert created == ()


def test_duty_skill_detector_skips_duplicate_pending(tmp_path):
    suggestion_store, detector, duties_dir, duty = _make_duty_skill_detector(tmp_path)
    _write_duty_runs(duties_dir, duty.duty_id, total=10)
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-duty-skill-1",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id=duty.duty_id,
            title="existing",
            reason="duplicate",
            expires_at=add_days_iso(now_iso(), 30),
        ),
    )

    created = detector.detect(tenant_id="tenant-1", worker_id="worker-1", duties_dir=duties_dir)

    assert created == ()


def test_duty_skill_detector_skips_recently_rejected(tmp_path):
    suggestion_store, detector, duties_dir, duty = _make_duty_skill_detector(tmp_path)
    _write_duty_runs(duties_dir, duty.duty_id, total=10)
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-duty-skill-1",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id=duty.duty_id,
            title="existing",
            reason="rejected",
            expires_at=add_days_iso(now_iso(), 30),
        ),
    )
    suggestion_store.resolve(
        "tenant-1",
        "worker-1",
        "sugg-duty-skill-1",
        status="rejected",
        resolved_by="user:test",
    )

    created = detector.detect(tenant_id="tenant-1", worker_id="worker-1", duties_dir=duties_dir)

    assert created == ()


def test_duty_skill_detector_skips_approved_source_via_store_create(tmp_path):
    suggestion_store, detector, duties_dir, duty = _make_duty_skill_detector(tmp_path)
    _write_duty_runs(duties_dir, duty.duty_id, total=10)
    suggestion_store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-duty-skill-approved",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id=duty.duty_id,
            title="approved",
            reason="already approved",
            expires_at=add_days_iso(now_iso(), 30),
        ),
    )
    suggestion_store.resolve(
        "tenant-1",
        "worker-1",
        "sugg-duty-skill-approved",
        status="approved",
        resolved_by="user:test",
    )

    created = detector.detect(tenant_id="tenant-1", worker_id="worker-1", duties_dir=duties_dir)

    assert created == ()


def test_duty_skill_detector_skips_low_success_rate(tmp_path):
    _, detector, duties_dir, duty = _make_duty_skill_detector(tmp_path)
    _write_duty_runs(duties_dir, duty.duty_id, total=20, failure_every=4)

    created = detector.detect(tenant_id="tenant-1", worker_id="worker-1", duties_dir=duties_dir)

    assert created == ()


def test_duty_skill_detector_skips_persistent_failure(tmp_path):
    _, detector, duties_dir, duty = _make_duty_skill_detector(tmp_path)
    _write_duty_runs(duties_dir, duty.duty_id, total=20, failure_every=1)

    created = detector.detect(tenant_id="tenant-1", worker_id="worker-1", duties_dir=duties_dir)

    assert created == ()
