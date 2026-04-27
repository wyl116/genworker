# edition: baseline
import os

from src.worker.lifecycle.models import SuggestionRecord, add_days_iso, now_iso
from src.worker.lifecycle.suggestion_store import SuggestionStore


def test_suggestion_store_pending_resolve_and_cooling(tmp_path):
    store = SuggestionStore(tmp_path)
    record = SuggestionRecord(
        suggestion_id="sugg-1",
        type="task_to_duty",
        source_entity_type="task_cluster",
        source_entity_id="cluster-1",
        title="cluster",
        reason="repeated",
        expires_at=add_days_iso(now_iso(), 30),
    )

    store.save_pending("tenant-1", "worker-1", record)

    pending = store.list_pending("tenant-1", "worker-1")
    assert len(pending) == 1
    assert pending[0].suggestion_id == "sugg-1"

    resolved = store.resolve(
        "tenant-1",
        "worker-1",
        "sugg-1",
        status="rejected",
        resolved_by="user:test",
        resolution_note="not needed",
    )
    assert resolved is not None
    assert resolved.status == "rejected"
    assert not store.list_pending("tenant-1", "worker-1")
    assert store.was_rejected_recently(
        "tenant-1",
        "worker-1",
        suggestion_type="task_to_duty",
        source_entity_id="cluster-1",
    )


def test_suggestion_store_auto_expires_pending(tmp_path):
    store = SuggestionStore(tmp_path)
    record = SuggestionRecord(
        suggestion_id="sugg-expire",
        type="goal_to_duty",
        source_entity_type="goal",
        source_entity_id="goal-1",
        title="goal",
        reason="done",
        expires_at="2000-01-01T00:00:00+00:00",
    )
    store.save_pending("tenant-1", "worker-1", record)

    expired = store.expire_pending("tenant-1", "worker-1")

    assert len(expired) == 1
    assert expired[0].status == "expired"
    assert not store.list_pending("tenant-1", "worker-1")


def test_suggestion_store_can_find_resolved_by_status(tmp_path):
    store = SuggestionStore(tmp_path)
    record = SuggestionRecord(
        suggestion_id="sugg-approved",
        type="goal_to_duty",
        source_entity_type="goal",
        source_entity_id="goal-1",
        title="goal",
        reason="done",
        expires_at=add_days_iso(now_iso(), 30),
    )
    store.save_pending("tenant-1", "worker-1", record)
    store.resolve(
        "tenant-1",
        "worker-1",
        "sugg-approved",
        status="approved",
        resolved_by="user:test",
    )

    approved = store.find_resolved(
        "tenant-1",
        "worker-1",
        suggestion_type="goal_to_duty",
        source_entity_id="goal-1",
        statuses=("approved",),
    )
    rejected = store.find_resolved(
        "tenant-1",
        "worker-1",
        suggestion_type="goal_to_duty",
        source_entity_id="goal-1",
        statuses=("rejected",),
    )

    assert approved is not None
    assert approved.status == "approved"
    assert rejected is None


def test_create_skips_already_approved_source(tmp_path):
    store = SuggestionStore(tmp_path)
    record = SuggestionRecord(
        suggestion_id="sugg-approved",
        type="goal_to_duty",
        source_entity_type="goal",
        source_entity_id="goal-1",
        title="goal",
        reason="done",
        expires_at=add_days_iso(now_iso(), 30),
    )
    store.save_pending("tenant-1", "worker-1", record)
    store.resolve(
        "tenant-1",
        "worker-1",
        "sugg-approved",
        status="approved",
        resolved_by="user:test",
    )

    duplicate = store.create(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-approved-2",
            type="goal_to_duty",
            source_entity_type="goal",
            source_entity_id="goal-1",
            title="goal-again",
            reason="done-again",
            expires_at=add_days_iso(now_iso(), 30),
        ),
    )

    assert duplicate is None
    assert store.list_pending("tenant-1", "worker-1") == ()


def test_creation_block_reason_reports_duplicate_and_rejected(tmp_path):
    store = SuggestionStore(tmp_path)
    pending = SuggestionRecord(
        suggestion_id="sugg-pending",
        type="goal_to_duty",
        source_entity_type="goal",
        source_entity_id="goal-pending",
        title="goal",
        reason="done",
        expires_at=add_days_iso(now_iso(), 30),
    )
    rejected = SuggestionRecord(
        suggestion_id="sugg-rejected",
        type="goal_to_duty",
        source_entity_type="goal",
        source_entity_id="goal-rejected",
        title="goal",
        reason="done",
        expires_at=add_days_iso(now_iso(), 30),
    )
    store.save_pending("tenant-1", "worker-1", pending)
    store.save_pending("tenant-1", "worker-1", rejected)
    store.resolve(
        "tenant-1",
        "worker-1",
        "sugg-rejected",
        status="rejected",
        resolved_by="user:test",
    )

    pending_reason = store.creation_block_reason(
        "tenant-1",
        "worker-1",
        suggestion_type="goal_to_duty",
        source_entity_id="goal-pending",
    )
    rejected_reason = store.creation_block_reason(
        "tenant-1",
        "worker-1",
        suggestion_type="goal_to_duty",
        source_entity_id="goal-rejected",
    )

    assert pending_reason == "duplicate pending suggestion"
    assert rejected_reason == "recently rejected"


def test_resolve_does_not_mutate_already_resolved_suggestion(tmp_path):
    store = SuggestionStore(tmp_path)
    record = SuggestionRecord(
        suggestion_id="sugg-approved",
        type="goal_to_duty",
        source_entity_type="goal",
        source_entity_id="goal-1",
        title="goal",
        reason="done",
        expires_at=add_days_iso(now_iso(), 30),
    )
    store.save_pending("tenant-1", "worker-1", record)
    approved = store.resolve(
        "tenant-1",
        "worker-1",
        "sugg-approved",
        status="approved",
        resolved_by="user:test",
    )

    assert approved is not None
    second = store.resolve(
        "tenant-1",
        "worker-1",
        "sugg-approved",
        status="rejected",
        resolved_by="user:other",
        resolution_note="should not overwrite",
    )

    assert second is None
    resolved = store.get("tenant-1", "worker-1", "sugg-approved")
    assert resolved is not None
    assert resolved.status == "approved"
    assert resolved.resolved_by == "user:test"


def test_get_pending_active_expires_stale_record(tmp_path):
    store = SuggestionStore(tmp_path)
    store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-stale",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id="duty-1",
            title="stale",
            reason="old",
            expires_at="2000-01-01T00:00:00+00:00",
        ),
    )

    record = store.get_pending_active("tenant-1", "worker-1", "sugg-stale")

    assert record is None
    resolved = store.get("tenant-1", "worker-1", "sugg-stale")
    assert resolved is not None
    assert resolved.status == "expired"


def test_get_state_distinguishes_pending_claimed_and_resolved(tmp_path):
    store = SuggestionStore(tmp_path)
    store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-pending-state",
            type="goal_to_duty",
            source_entity_type="goal",
            source_entity_id="goal-pending-state",
            title="pending",
            reason="pending",
            expires_at=add_days_iso(now_iso(), 30),
        ),
    )
    store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-claimed-state",
            type="goal_to_duty",
            source_entity_type="goal",
            source_entity_id="goal-claimed-state",
            title="claimed",
            reason="claimed",
            expires_at=add_days_iso(now_iso(), 30),
        ),
    )
    store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-approved-state",
            type="goal_to_duty",
            source_entity_type="goal",
            source_entity_id="goal-approved-state",
            title="approved",
            reason="approved",
            expires_at=add_days_iso(now_iso(), 30),
        ),
    )

    store.claim_pending("tenant-1", "worker-1", "sugg-claimed-state")
    store.resolve(
        "tenant-1",
        "worker-1",
        "sugg-approved-state",
        status="approved",
        resolved_by="user:test",
    )

    assert store.get_state("tenant-1", "worker-1", "sugg-pending-state") == "pending"
    assert store.get_state("tenant-1", "worker-1", "sugg-claimed-state") == "claimed"
    assert store.get_state("tenant-1", "worker-1", "sugg-approved-state") == "approved"
    assert store.get_state("tenant-1", "worker-1", "missing-state") == "missing"


def test_claim_and_release_pending_suggestion(tmp_path):
    store = SuggestionStore(tmp_path)
    store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-claim",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id="duty-1",
            title="claim",
            reason="race",
            expires_at=add_days_iso(now_iso(), 30),
        ),
    )

    claimed = store.claim_pending("tenant-1", "worker-1", "sugg-claim")

    assert claimed is not None
    assert claimed.suggestion_id == "sugg-claim"
    assert claimed.claim_token
    assert store.get_pending_active("tenant-1", "worker-1", "sugg-claim") is None

    released = store.release_claim(
        "tenant-1",
        "worker-1",
        "sugg-claim",
        claim_token=claimed.claim_token,
    )

    assert released is not None
    assert released.suggestion_id == "sugg-claim"
    assert released.claim_token == ""
    assert store.get_pending_active("tenant-1", "worker-1", "sugg-claim") is not None


def test_release_claim_rejects_wrong_token(tmp_path):
    store = SuggestionStore(tmp_path)
    store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-token-release",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id="duty-1",
            title="claim",
            reason="race",
            expires_at=add_days_iso(now_iso(), 30),
        ),
    )

    claimed = store.claim_pending("tenant-1", "worker-1", "sugg-token-release")

    assert claimed is not None
    released = store.release_claim(
        "tenant-1",
        "worker-1",
        "sugg-token-release",
        claim_token="wrong-token",
    )

    assert released is None
    assert store.get_state("tenant-1", "worker-1", "sugg-token-release") == "claimed"


def test_resolve_rejects_wrong_claim_token(tmp_path):
    store = SuggestionStore(tmp_path)
    store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-token-resolve",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id="duty-1",
            title="claim",
            reason="race",
            expires_at=add_days_iso(now_iso(), 30),
        ),
    )

    claimed = store.claim_pending("tenant-1", "worker-1", "sugg-token-resolve")

    assert claimed is not None
    resolved = store.resolve(
        "tenant-1",
        "worker-1",
        "sugg-token-resolve",
        status="approved",
        resolved_by="user:test",
        claim_token="wrong-token",
    )

    assert resolved is None
    assert store.get_state("tenant-1", "worker-1", "sugg-token-resolve") == "claimed"


def test_stale_claim_is_recovered_to_pending(tmp_path):
    store = SuggestionStore(tmp_path)
    store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-stale-claim",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id="duty-1",
            title="claim",
            reason="recover",
            expires_at=add_days_iso(now_iso(), 30),
        ),
    )

    claimed = store.claim_pending("tenant-1", "worker-1", "sugg-stale-claim")

    assert claimed is not None
    claimed_file = (
        tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1"
        / "lifecycle" / "suggestions" / "claimed" / "sugg-stale-claim.json"
    )
    store._write_record(
        claimed_file,
        claimed.__class__.from_dict({
            **claimed.to_dict(),
            "claimed_at": "2000-01-01T00:00:00+00:00",
        }),
    )
    stale_ts = claimed_file.stat().st_mtime - (SuggestionStore._CLAIM_TIMEOUT_SECONDS + 5)
    os.utime(claimed_file, (stale_ts, stale_ts))

    recovered = store.get_pending_active("tenant-1", "worker-1", "sugg-stale-claim")

    assert recovered is not None
    assert recovered.suggestion_id == "sugg-stale-claim"
    assert recovered.claim_token == ""
    assert recovered.claimed_at == ""


def test_touch_claim_keeps_recent_claimed_at_even_if_mtime_is_stale(tmp_path):
    store = SuggestionStore(tmp_path)
    store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-touch-claim",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id="duty-1",
            title="claim",
            reason="touch",
            expires_at=add_days_iso(now_iso(), 30),
        ),
    )

    claimed = store.claim_pending("tenant-1", "worker-1", "sugg-touch-claim")

    assert claimed is not None
    assert store.touch_claim(
        "tenant-1",
        "worker-1",
        "sugg-touch-claim",
        claim_token=claimed.claim_token,
    )
    claimed_file = (
        tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1"
        / "lifecycle" / "suggestions" / "claimed" / "sugg-touch-claim.json"
    )
    stale_ts = claimed_file.stat().st_mtime - (SuggestionStore._CLAIM_TIMEOUT_SECONDS + 5)
    os.utime(claimed_file, (stale_ts, stale_ts))

    assert store.get_state("tenant-1", "worker-1", "sugg-touch-claim") == "claimed"


def test_mark_approval_applied_persists_checkpoint_and_survives_release(tmp_path):
    store = SuggestionStore(tmp_path)
    store.save_pending(
        "tenant-1",
        "worker-1",
        SuggestionRecord(
            suggestion_id="sugg-approval-checkpoint",
            type="duty_to_skill",
            source_entity_type="duty",
            source_entity_id="duty-1",
            title="checkpoint",
            reason="apply",
            expires_at=add_days_iso(now_iso(), 30),
        ),
    )

    claimed = store.claim_pending("tenant-1", "worker-1", "sugg-approval-checkpoint")

    assert claimed is not None
    checkpointed = store.mark_approval_applied(
        "tenant-1",
        "worker-1",
        "sugg-approval-checkpoint",
        claim_token=claimed.claim_token,
        summary="已创建 Skill 'skill-duty-1' 并写入 SKILL.md。",
        artifact_ref="skill:skill-duty-1",
    )

    assert checkpointed is not None
    assert checkpointed.approval_stage == "materialized"
    assert checkpointed.approval_summary
    assert checkpointed.approval_artifact_ref == "skill:skill-duty-1"
    released = store.release_claim(
        "tenant-1",
        "worker-1",
        "sugg-approval-checkpoint",
        claim_token=claimed.claim_token,
    )

    assert released is not None
    assert released.approval_stage == "materialized"
    assert released.approval_artifact_ref == "skill:skill-duty-1"
