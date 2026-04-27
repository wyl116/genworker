# edition: baseline
from src.bootstrap.scheduler_init import build_goal_health_job_id
from src.runtime.scheduler_runtime import build_worker_recurring_job_ids


def test_goal_health_job_id_includes_worker_id():
    assert build_goal_health_job_id("worker-a", "goal-1") != build_goal_health_job_id("worker-b", "goal-1")


def test_learning_job_ids_are_unique_per_worker():
    worker_a = "a1"
    worker_b = "b1"
    ids = {
        *build_worker_recurring_job_ids(worker_a).values(),
        *build_worker_recurring_job_ids(worker_b).values(),
    }
    assert len(ids) == 14


def test_worker_recurring_job_ids_match_canonical_names():
    worker_id = "worker-1"
    assert build_worker_recurring_job_ids(worker_id) == {
        "profile_update": "system:profile-update:worker-1",
        "crystallization": "system:crystallization:worker-1",
        "task_pattern": "system:task-pattern:worker-1",
        "goal_completion": "system:goal-completion-advisor:worker-1",
        "duty_drift": "system:duty-drift:worker-1",
        "sharing_cycle": "system:sharing-cycle:worker-1",
        "duty_to_skill": "system:duty-to-skill:worker-1",
    }
