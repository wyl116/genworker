# edition: baseline
from src.autonomy.inbox import InboxItem
from src.worker.heartbeat.strategy import HeartbeatStrategy, HeartbeatStrategyConfig
from src.worker.models import WorkerHeartbeatConfig


def test_goal_check_escalates_to_task():
    strategy = HeartbeatStrategy()
    action = strategy.decide_action(
        InboxItem(
            tenant_id="demo",
            worker_id="w1",
            source_type="goal_check",
            event_type="goal.health_check_detected",
            payload={
                "goal_title": "Recover Pipeline",
                "recommended_action": "escalate",
                "deviation_score": 0.6,
            },
        )
    )

    assert action.kind == "task"
    assert "[Goal Health Follow-up] Recover Pipeline" in action.task_description


def test_goal_check_escalates_to_isolated_run():
    strategy = HeartbeatStrategy()
    action = strategy.decide_action(
        InboxItem(
            tenant_id="demo",
            worker_id="w1",
            source_type="goal_check",
            event_type="goal.health_check_detected",
            payload={
                "goal_title": "Critical Migration",
                "recommended_action": "replan",
                "deviation_score": 0.95,
            },
        )
    )

    assert action.kind == "isolated"
    assert "[Goal Health Follow-up] Critical Migration" in action.task_description


def test_email_followup_uses_generated_task():
    strategy = HeartbeatStrategy()
    action = strategy.decide_action(
        InboxItem(
            tenant_id="demo",
            worker_id="w1",
            source_type="email",
            event_type="external.email_received",
            payload={
                "subject": "Need response",
                "from": "alice@example.com",
                "content": "please help",
                "requires_follow_up": True,
            },
        )
    )

    assert action.kind == "task"
    assert "[Email Follow-up] Need response" in action.task_description


def test_explicit_run_mode_wins():
    strategy = HeartbeatStrategy()
    action = strategy.decide_action(
        InboxItem(
            tenant_id="demo",
            worker_id="w1",
            source_type="email",
            event_type="external.email_received",
            payload={
                "run_mode": "isolated",
                "task_description": "do a deep forensic analysis",
            },
        )
    )

    assert action.kind == "isolated"
    assert action.task_description == "do a deep forensic analysis"


def test_unknown_item_defaults_to_summary():
    strategy = HeartbeatStrategy()
    action = strategy.decide_action(
        InboxItem(
            tenant_id="demo",
            worker_id="w1",
            source_type="unknown",
            event_type="unknown.event",
            payload={"message": "noop"},
        )
    )

    assert action.kind == "summary"
    assert action.task_description == ""


def test_custom_threshold_can_downgrade_isolated_run():
    strategy = HeartbeatStrategy(
        config=HeartbeatStrategyConfig(
            goal_task_actions=frozenset({"escalate", "replan"}),
            goal_isolated_actions=frozenset({"deep_review"}),
            goal_isolated_deviation_threshold=0.99,
        )
    )
    action = strategy.decide_action(
        InboxItem(
            tenant_id="demo",
            worker_id="w1",
            source_type="goal_check",
            event_type="goal.health_check_detected",
            payload={
                "goal_title": "Critical Migration",
                "recommended_action": "replan",
                "deviation_score": 0.95,
            },
        )
    )

    assert action.kind == "task"
    assert "[Goal Health Follow-up] Critical Migration" in action.task_description


def test_config_from_settings_parses_csv_values():
    class _Settings:
        heartbeat_goal_task_actions = "escalate, recover"
        heartbeat_goal_isolated_actions = "replan, deep_review"
        heartbeat_goal_isolated_deviation_threshold = 0.8

    config = HeartbeatStrategyConfig.from_settings(_Settings())

    assert config.goal_task_actions == frozenset({"escalate", "recover"})
    assert config.goal_isolated_actions == frozenset({"replan", "deep_review"})
    assert config.goal_isolated_deviation_threshold == 0.8


def test_worker_overrides_can_replace_global_strategy():
    base = HeartbeatStrategyConfig(
        goal_task_actions=frozenset({"escalate"}),
        goal_isolated_actions=frozenset({"replan"}),
        goal_isolated_deviation_threshold=0.9,
    )
    merged = base.with_worker_overrides(
        WorkerHeartbeatConfig(
            goal_task_actions=("recover",),
            goal_isolated_actions=("deep_review",),
            goal_isolated_deviation_threshold=0.98,
        )
    )

    assert merged.goal_task_actions == frozenset({"recover"})
    assert merged.goal_isolated_actions == frozenset({"deep_review"})
    assert merged.goal_isolated_deviation_threshold == 0.98
