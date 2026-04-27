# edition: baseline
"""
Tests for WorkerScheduler - concurrency, quota, priority queue.
"""
import asyncio

import pytest

from src.events.bus import EventBus, Subscription
from src.streaming.events import ErrorEvent, RunFinishedEvent, TextMessageEvent
from src.worker.scheduler import (
    QuotaExhaustedError,
    SchedulerConfig,
    TriggerPriority,
    WorkerScheduler,
)


class TestSchedulerConfig:
    def test_defaults(self):
        config = SchedulerConfig()
        assert config.max_concurrent_tasks == 5
        assert config.daily_task_quota == 100
        assert config.goal_check_enabled is True
        assert config.max_task_retries == 2

    def test_custom(self):
        config = SchedulerConfig(
            max_concurrent_tasks=3,
            daily_task_quota=50,
            goal_check_enabled=False,
            max_task_retries=1,
        )
        assert config.max_concurrent_tasks == 3
        assert config.daily_task_quota == 50
        assert config.max_task_retries == 1


class TestTriggerPriority:
    def test_duty_highest(self):
        p = TriggerPriority()
        assert p.DUTY < p.PERSONA_TRIGGER < p.GOAL

    def test_priority_values(self):
        p = TriggerPriority()
        assert p.DUTY == 10
        assert p.PERSONA_TRIGGER == 20
        assert p.GOAL == 30


class TestWorkerScheduler:
    @pytest.mark.asyncio
    async def test_submit_within_limits(self):
        config = SchedulerConfig(max_concurrent_tasks=5, daily_task_quota=100)
        scheduler = WorkerScheduler(config=config)

        job = {"task": "test", "tenant_id": "t1"}
        result = await scheduler.submit_task(job, priority=10)
        assert result is True
        assert scheduler.active_count == 1

    @pytest.mark.asyncio
    async def test_daily_quota_rejection(self):
        config = SchedulerConfig(max_concurrent_tasks=100, daily_task_quota=3)
        scheduler = WorkerScheduler(config=config)

        for i in range(3):
            result = await scheduler.submit_task(
                {"task": f"task-{i}", "tenant_id": "t1"}, priority=10,
            )
            assert result is True

        # Fourth task should be rejected
        result = await scheduler.submit_task(
            {"task": "task-4", "tenant_id": "t1"}, priority=10,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_concurrent_limit_queues_task(self):
        config = SchedulerConfig(max_concurrent_tasks=1, daily_task_quota=100)

        # Track execution order
        execution_order = []
        completed = asyncio.Event()

        class FakeRouter:
            async def route_stream(self, task, tenant_id, worker_id=None):
                execution_order.append(task)
                # Simulate some work
                await asyncio.sleep(0.05)
                yield type("Event", (), {"content": "done", "run_id": "r1"})()

        scheduler = WorkerScheduler(config=config, worker_router=FakeRouter())

        # Submit two tasks - first runs, second queues
        result1 = await scheduler.submit_task(
            {"task": "task-1", "tenant_id": "t1"}, priority=20,
        )
        assert result1 is True
        assert scheduler.active_count == 1

        result2 = await scheduler.submit_task(
            {"task": "task-2", "tenant_id": "t1"}, priority=10,
        )
        assert result2 is True
        assert scheduler.queue_size == 1

        # Wait for both tasks to complete
        await asyncio.sleep(0.2)
        assert scheduler.active_count == 0
        assert scheduler.queue_size == 0

    @pytest.mark.asyncio
    async def test_priority_ordering_in_queue(self):
        config = SchedulerConfig(max_concurrent_tasks=1, daily_task_quota=100)

        execution_order = []

        class SlowRouter:
            async def route_stream(self, task, tenant_id, worker_id=None):
                execution_order.append(task)
                await asyncio.sleep(0.05)
                yield type("Event", (), {"content": "done", "run_id": "r1"})()

        scheduler = WorkerScheduler(config=config, worker_router=SlowRouter())

        # First task starts immediately
        await scheduler.submit_task(
            {"task": "blocking", "tenant_id": "t1"}, priority=10,
        )

        # Queue tasks with different priorities - duty(10) > goal(30)
        await scheduler.submit_task(
            {"task": "goal-task", "tenant_id": "t1"}, priority=30,
        )
        await scheduler.submit_task(
            {"task": "duty-task", "tenant_id": "t1"}, priority=10,
        )

        # duty-task should be dequeued before goal-task
        assert scheduler.queue_size == 2

        # Wait for all to complete
        await asyncio.sleep(0.3)

        # Verify duty-task ran before goal-task
        assert execution_order[0] == "blocking"
        assert execution_order[1] == "duty-task"
        assert execution_order[2] == "goal-task"

    @pytest.mark.asyncio
    async def test_daily_count_tracking(self):
        config = SchedulerConfig(max_concurrent_tasks=10, daily_task_quota=100)
        scheduler = WorkerScheduler(config=config)

        await scheduler.submit_task(
            {"task": "t1", "tenant_id": "t1"}, priority=10,
        )
        await scheduler.submit_task(
            {"task": "t2", "tenant_id": "t1"}, priority=10,
        )

        assert scheduler.daily_count == 2

    @pytest.mark.asyncio
    async def test_task_completion_dequeues_next(self):
        config = SchedulerConfig(max_concurrent_tasks=1, daily_task_quota=100)

        completed_tasks = []

        class QuickRouter:
            async def route_stream(self, task, tenant_id, worker_id=None):
                completed_tasks.append(task)
                yield type("Event", (), {"content": "done", "run_id": "r1"})()

        scheduler = WorkerScheduler(config=config, worker_router=QuickRouter())

        # Submit 3 tasks
        for i in range(3):
            await scheduler.submit_task(
                {"task": f"task-{i}", "tenant_id": "t1"}, priority=10,
            )

        # Wait for completion
        await asyncio.sleep(0.2)

        assert len(completed_tasks) == 3
        assert scheduler.active_count == 0
        assert scheduler.queue_size == 0

    @pytest.mark.asyncio
    async def test_isolated_run_completion_publishes_event(self):
        from src.events.bus import EventBus, Subscription

        captured = []

        async def _on_completed(event):
            captured.append(dict(event.payload))

        event_bus = EventBus()
        event_bus.subscribe(
            Subscription(
                handler_id="test-completed",
                event_type="isolated_run.completed",
                tenant_id="demo",
                handler=_on_completed,
            )
        )

        class SuccessRouter:
            async def route_stream(self, task, tenant_id, worker_id=None, manifest=None):
                yield type(
                    "Event",
                    (),
                    {
                        "content": "done",
                        "run_id": "run-1",
                        "event_type": "TEXT_MESSAGE",
                    },
                )()

        scheduler = WorkerScheduler(
            config=SchedulerConfig(max_concurrent_tasks=1, daily_task_quota=10),
            worker_router=SuccessRouter(),
            event_bus=event_bus,
        )

        from src.worker.task import create_task_manifest

        manifest = create_task_manifest(
            worker_id="w1",
            tenant_id="demo",
            task_description="deep work",
            main_session_key="main:w1",
        )
        await scheduler.submit_task(
            {
                "task": "deep work",
                "tenant_id": "demo",
                "worker_id": "w1",
                "manifest": manifest,
            },
            priority=10,
        )

        await asyncio.sleep(0.1)

        assert captured
        assert captured[0]["main_session_key"] == "main:w1"

    @pytest.mark.asyncio
    async def test_task_completed_event_contains_thread_id(self):
        from src.events.bus import EventBus, Subscription

        captured = []

        async def _on_completed(event):
            captured.append(dict(event.payload))

        event_bus = EventBus()
        event_bus.subscribe(
            Subscription(
                handler_id="test-task-completed",
                event_type="task.completed",
                tenant_id="demo",
                handler=_on_completed,
            )
        )

        class SuccessRouter:
            async def route_stream(self, task, tenant_id, worker_id=None, manifest=None):
                yield type(
                    "Event",
                    (),
                    {
                        "content": "done",
                        "run_id": "run-1",
                        "event_type": "TEXT_MESSAGE",
                    },
                )()

        scheduler = WorkerScheduler(
            config=SchedulerConfig(max_concurrent_tasks=1, daily_task_quota=10),
            worker_router=SuccessRouter(),
            event_bus=event_bus,
        )

        from src.worker.task import create_task_manifest

        manifest = create_task_manifest(
            worker_id="w1",
            tenant_id="demo",
            task_description="deep work",
        )
        await scheduler.submit_task(
            {
                "task": "deep work",
                "tenant_id": "demo",
                "worker_id": "w1",
                "manifest": manifest,
                "thread_id": "im:feishu:oc_123:ou_1",
            },
            priority=10,
        )

        await asyncio.sleep(0.1)

        assert captured
        assert captured[0]["thread_id"] == "im:feishu:oc_123:ou_1"

    @pytest.mark.asyncio
    async def test_task_failed_event_contains_thread_id_without_session_id(self):
        from src.events.bus import EventBus, Subscription

        captured = []

        async def _on_failed(event):
            captured.append(dict(event.payload))

        event_bus = EventBus()
        event_bus.subscribe(
            Subscription(
                handler_id="test-task-failed",
                event_type="task.failed",
                tenant_id="demo",
                handler=_on_failed,
            )
        )

        class FailedRouter:
            async def route_stream(self, task, tenant_id, worker_id=None, manifest=None):
                yield type(
                    "Event",
                    (),
                    {
                        "content": "",
                        "run_id": "run-1",
                        "event_type": "ERROR",
                        "message": "boom",
                    },
                )()

        scheduler = WorkerScheduler(
            config=SchedulerConfig(max_concurrent_tasks=1, daily_task_quota=10),
            worker_router=FailedRouter(),
            event_bus=event_bus,
        )

        from src.worker.task import create_task_manifest

        manifest = create_task_manifest(
            worker_id="w1",
            tenant_id="demo",
            task_description="deep work",
        )
        await scheduler.submit_task(
            {
                "task": "deep work",
                "tenant_id": "demo",
                "worker_id": "w1",
                "manifest": manifest,
                "thread_id": "im:feishu:oc_123:ou_1",
            },
            priority=10,
        )

        await asyncio.sleep(0.1)

        assert captured
        assert captured[0]["thread_id"] == "im:feishu:oc_123:ou_1"
        assert captured[0]["error_message"] == "boom"

    @pytest.mark.asyncio
    async def test_scheduler_retries_and_keeps_completion_future_pending(self):
        attempts = 0
        completion_future = asyncio.get_running_loop().create_future()
        finish_gate = asyncio.Event()

        async def flaky_runner():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError(f"boom-{attempts}")
            await finish_gate.wait()
            return {"success": True, "content": "ok"}

        scheduler = WorkerScheduler(
            config=SchedulerConfig(
                max_concurrent_tasks=1,
                daily_task_quota=10,
                max_task_retries=2,
            ),
        )

        accepted = await scheduler.submit_task(
            {"runner": flaky_runner, "completion_future": completion_future},
            priority=10,
        )
        assert accepted is True
        await asyncio.sleep(0.02)
        assert completion_future.done() is False

        finish_gate.set()
        result = await asyncio.wait_for(completion_future, timeout=0.5)
        assert result["success"] is True
        assert attempts == 3
        assert scheduler.active_count == 0
        assert scheduler.queue_size == 0

    @pytest.mark.asyncio
    async def test_scheduler_dead_letters_after_retry_exhaustion(self):
        class RecordingDeadLetterStore:
            def __init__(self) -> None:
                self.entries = []

            async def add(self, entry):
                self.entries.append(entry)

        dead_letters = RecordingDeadLetterStore()
        completion_future = asyncio.get_running_loop().create_future()
        event_bus = EventBus()
        captured = []

        async def _on_dead_lettered(event):
            captured.append(dict(event.payload))

        event_bus.subscribe(
            Subscription(
                handler_id="test-dead-lettered",
                event_type="task.dead_lettered",
                tenant_id="demo",
                handler=_on_dead_lettered,
            )
        )

        async def failing_runner():
            raise RuntimeError("always fails")

        scheduler = WorkerScheduler(
            config=SchedulerConfig(
                max_concurrent_tasks=1,
                daily_task_quota=10,
                max_task_retries=1,
            ),
            event_bus=event_bus,
            dead_letter_store=dead_letters,
        )

        await scheduler.submit_task(
            {
                "runner": failing_runner,
                "completion_future": completion_future,
                "tenant_id": "demo",
                "worker_id": "w1",
                "task": "broken task",
            },
            priority=10,
        )

        result = await asyncio.wait_for(completion_future, timeout=0.5)
        assert result["success"] is False
        assert len(dead_letters.entries) == 1
        assert dead_letters.entries[0].task_description == "broken task"
        assert captured[0]["worker_id"] == "w1"
        assert captured[0]["task_description"] == "broken task"
        assert captured[0]["total_attempts"] == 2

    @pytest.mark.asyncio
    async def test_isolated_run_failure_publishes_failed_event(self):
        from src.events.bus import EventBus, Subscription

        captured = []

        async def _on_failed(event):
            captured.append(dict(event.payload))

        event_bus = EventBus()
        event_bus.subscribe(
            Subscription(
                handler_id="test-failed",
                event_type="isolated_run.failed",
                tenant_id="demo",
                handler=_on_failed,
            )
        )

        class FailedRouter:
            async def route_stream(self, task, tenant_id, worker_id=None, manifest=None):
                yield type(
                    "Event",
                    (),
                    {
                        "content": "",
                        "run_id": "run-1",
                        "event_type": "ERROR",
                        "message": "boom",
                    },
                )()

        scheduler = WorkerScheduler(
            config=SchedulerConfig(max_concurrent_tasks=1, daily_task_quota=10),
            worker_router=FailedRouter(),
            event_bus=event_bus,
        )

        from src.worker.task import create_task_manifest

        manifest = create_task_manifest(
            worker_id="w1",
            tenant_id="demo",
            task_description="deep work",
            main_session_key="main:w1",
        )
        await scheduler.submit_task(
            {
                "task": "deep work",
                "tenant_id": "demo",
                "worker_id": "w1",
                "manifest": manifest,
            },
            priority=10,
        )

        await asyncio.sleep(0.1)

        assert captured
        assert captured[0]["main_session_key"] == "main:w1"
        assert captured[0]["error_message"] == "boom"

    @pytest.mark.asyncio
    async def test_submit_langgraph_resume_marks_consumed_on_success(self):
        class _InboxStore:
            def __init__(self) -> None:
                self.consumed = []
                self.errors = []

            async def mark_consumed(self, inbox_ids, *, tenant_id="", worker_id=""):
                self.consumed.append((tuple(inbox_ids), tenant_id, worker_id))

            async def mark_error(self, inbox_id, *, reason, tenant_id="", worker_id=""):
                self.errors.append((inbox_id, reason, tenant_id, worker_id))

        class _LangGraphEngine:
            async def resume(self, **kwargs):
                yield TextMessageEvent(run_id=kwargs["thread_id"], content="done")
                yield RunFinishedEvent(run_id=kwargs["thread_id"], success=True)

        inbox_store = _InboxStore()
        scheduler = WorkerScheduler(
            config=SchedulerConfig(max_concurrent_tasks=1, daily_task_quota=10),
            engine_dispatcher=type("Dispatcher", (), {"langgraph_engine": _LangGraphEngine()})(),
            inbox_store=inbox_store,
        )

        accepted = await scheduler.submit_langgraph_resume(
            {
                "tenant_id": "demo",
                "worker_id": "w1",
                "thread_id": "thread-1",
                "skill_id": "approval-flow",
                "decision": {"approved": True},
                "expected_digest": "digest-1",
                "inbox_id": "inbox-1",
            },
            priority=10,
        )

        assert accepted is True
        await asyncio.sleep(0.05)

        assert inbox_store.consumed == [(("inbox-1",), "demo", "w1")]
        assert inbox_store.errors == []

    @pytest.mark.asyncio
    async def test_submit_langgraph_resume_marks_error_on_failed_events(self):
        class _InboxStore:
            def __init__(self) -> None:
                self.consumed = []
                self.errors = []

            async def mark_consumed(self, inbox_ids, *, tenant_id="", worker_id=""):
                self.consumed.append((tuple(inbox_ids), tenant_id, worker_id))

            async def mark_error(self, inbox_id, *, reason, tenant_id="", worker_id=""):
                self.errors.append((inbox_id, reason, tenant_id, worker_id))

        class _LangGraphEngine:
            async def resume(self, **kwargs):
                yield ErrorEvent(
                    run_id=kwargs["thread_id"],
                    code="LANGGRAPH_STATE_DRIFT",
                    message="digest mismatch",
                )
                yield RunFinishedEvent(
                    run_id=kwargs["thread_id"],
                    success=False,
                    stop_reason="state_drift",
                )

        inbox_store = _InboxStore()
        scheduler = WorkerScheduler(
            config=SchedulerConfig(max_concurrent_tasks=1, daily_task_quota=10),
            engine_dispatcher=type("Dispatcher", (), {"langgraph_engine": _LangGraphEngine()})(),
            inbox_store=inbox_store,
        )

        accepted = await scheduler.submit_langgraph_resume(
            {
                "tenant_id": "demo",
                "worker_id": "w1",
                "thread_id": "thread-1",
                "skill_id": "approval-flow",
                "decision": {"approved": True},
                "expected_digest": "digest-1",
                "inbox_id": "inbox-1",
            },
            priority=10,
        )

        assert accepted is True
        await asyncio.sleep(0.05)

        assert inbox_store.consumed == []
        assert inbox_store.errors == [("inbox-1", "digest mismatch", "demo", "w1")]
