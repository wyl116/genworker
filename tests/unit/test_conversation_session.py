# edition: baseline
"""
Unit tests for ConversationSession and ChatMessage models.

Tests immutability, append_message, add_spawned_task, serialization.
"""
import json

import pytest

from src.conversation.models import ChatMessage, ConversationSession


class TestChatMessageFrozen:
    """ChatMessage must be a frozen dataclass."""

    def test_immutable(self):
        msg = ChatMessage(role="user", content="hello")
        with pytest.raises(AttributeError):
            msg.content = "modified"

    def test_defaults(self):
        msg = ChatMessage(role="user", content="hi")
        assert msg.role == "user"
        assert msg.content == "hi"
        assert msg.timestamp  # auto-generated
        assert msg.skill_id is None
        assert msg.spawned_task_id is None

    def test_with_optional_fields(self):
        msg = ChatMessage(
            role="assistant",
            content="result",
            skill_id="data-analysis",
            spawned_task_id="task-123",
        )
        assert msg.skill_id == "data-analysis"
        assert msg.spawned_task_id == "task-123"


class TestConversationSessionFrozen:
    """ConversationSession must be a frozen dataclass."""

    def test_immutable(self):
        session = ConversationSession(
            session_id="s1",
            thread_id="t1",
            tenant_id="demo",
            worker_id="w1",
            ttl_seconds=900,
        )
        with pytest.raises(AttributeError):
            session.thread_id = "modified"

    def test_messages_tuple_immutable(self):
        session = ConversationSession(
            session_id="s1",
            thread_id="t1",
            tenant_id="demo",
            worker_id="w1",
        )
        assert session.messages == ()
        with pytest.raises(AttributeError):
            session.messages = (ChatMessage(role="user", content="x"),)


class TestAppendMessage:
    """append_message returns a new session; original is unchanged."""

    def test_returns_new_object(self):
        original = ConversationSession(
            session_id="s1",
            thread_id="t1",
            tenant_id="demo",
            worker_id="w1",
        )
        msg = ChatMessage(role="user", content="hello")
        updated = original.append_message(msg)

        # New object
        assert updated is not original
        # Original unchanged
        assert len(original.messages) == 0
        # Updated has the message
        assert len(updated.messages) == 1
        assert updated.messages[0].content == "hello"

    def test_preserves_existing_messages(self):
        msg1 = ChatMessage(role="user", content="first")
        session = ConversationSession(
            session_id="s1",
            thread_id="t1",
            tenant_id="demo",
            worker_id="w1",
            messages=(msg1,),
        )

        msg2 = ChatMessage(role="assistant", content="second")
        updated = session.append_message(msg2)

        assert len(updated.messages) == 2
        assert updated.messages[0].content == "first"
        assert updated.messages[1].content == "second"

    def test_updates_last_active_at(self):
        original = ConversationSession(
            session_id="s1",
            thread_id="t1",
            tenant_id="demo",
            worker_id="w1",
            last_active_at="2026-01-01T00:00:00+00:00",
        )
        msg = ChatMessage(
            role="user",
            content="hello",
            timestamp="2026-04-04T12:00:00+00:00",
        )
        updated = original.append_message(msg)

        assert updated.last_active_at == "2026-04-04T12:00:00+00:00"
        assert original.last_active_at == "2026-01-01T00:00:00+00:00"


class TestAddSpawnedTask:
    """add_spawned_task returns a new session; original is unchanged."""

    def test_returns_new_object(self):
        original = ConversationSession(
            session_id="s1",
            thread_id="t1",
            tenant_id="demo",
            worker_id="w1",
        )
        updated = original.add_spawned_task("task-001")

        assert updated is not original
        assert len(original.spawned_tasks) == 0
        assert len(updated.spawned_tasks) == 1
        assert updated.spawned_tasks[0] == "task-001"

    def test_preserves_existing_tasks(self):
        session = ConversationSession(
            session_id="s1",
            thread_id="t1",
            tenant_id="demo",
            worker_id="w1",
            spawned_tasks=("task-001",),
        )
        updated = session.add_spawned_task("task-002")

        assert len(updated.spawned_tasks) == 2
        assert updated.spawned_tasks == ("task-001", "task-002")


class TestSerialization:
    """to_dict / from_dict round-trip."""

    def test_round_trip(self):
        msg = ChatMessage(
            role="user",
            content="hello",
            timestamp="2026-04-04T10:00:00+00:00",
            skill_id="general",
        )
        session = ConversationSession(
            session_id="s1",
            thread_id="t1",
            tenant_id="demo",
            worker_id="w1",
            messages=(msg,),
            spawned_tasks=("task-001",),
            created_at="2026-04-04T09:00:00+00:00",
            last_active_at="2026-04-04T10:00:00+00:00",
            ttl_seconds=900,
            metadata=(("key1", "val1"),),
        )

        data = session.to_dict()
        restored = ConversationSession.from_dict(data)

        assert restored.session_id == "s1"
        assert restored.thread_id == "t1"
        assert restored.tenant_id == "demo"
        assert restored.worker_id == "w1"
        assert len(restored.messages) == 1
        assert restored.messages[0].content == "hello"
        assert restored.messages[0].skill_id == "general"
        assert restored.spawned_tasks == ("task-001",)
        assert restored.ttl_seconds == 900
        assert restored.metadata == (("key1", "val1"),)

    def test_json_serializable(self):
        session = ConversationSession(
            session_id="s1",
            thread_id="t1",
            tenant_id="demo",
            worker_id="w1",
        )
        data = session.to_dict()
        json_str = json.dumps(data, ensure_ascii=False)
        assert isinstance(json_str, str)

    def test_from_dict_empty(self):
        session = ConversationSession.from_dict({})
        assert session.session_id == ""
        assert session.messages == ()
        assert session.spawned_tasks == ()
        assert session.session_type == "thread"
        assert session.main_session_key is None
        assert session.inbox_cursor is None

    def test_backward_compatible_defaults_for_new_fields(self):
        session = ConversationSession.from_dict(
            {
                "session_id": "s1",
                "thread_id": "t1",
                "tenant_id": "demo",
                "worker_id": "w1",
                "messages": [],
                "spawned_tasks": [],
                "created_at": "2025-01-01T00:00:00+00:00",
                "last_active_at": "2025-01-01T00:00:00+00:00",
                "metadata": [],
            }
        )
        assert session.session_type == "thread"
        assert session.main_session_key is None
        assert session.inbox_cursor is None
        assert session.last_heartbeat_at is None
        assert session.open_concerns == ()
        assert session.task_refs == ()


class TestSessionManager:
    """Tests for SessionManager lifecycle."""

    @pytest.mark.asyncio
    async def test_get_or_create_new_session(self):
        from src.conversation.session_manager import SessionManager
        from src.conversation.session_store import FileSessionStore

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = FileSessionStore(Path(tmp))
            manager = SessionManager(store=store, ttl_seconds=3600)

            session = await manager.get_or_create(
                thread_id="t1",
                tenant_id="demo",
                worker_id="w1",
            )
            assert session.thread_id == "t1"
            assert session.tenant_id == "demo"
            assert session.session_id  # auto-generated

    @pytest.mark.asyncio
    async def test_get_or_create_applies_per_session_ttl_and_metadata(self):
        from src.conversation.session_manager import SessionManager
        from src.conversation.session_store import FileSessionStore

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = FileSessionStore(Path(tmp))
            manager = SessionManager(store=store, ttl_seconds=3600)

            session = await manager.get_or_create(
                thread_id="t1",
                tenant_id="demo",
                worker_id="w1",
                ttl_seconds=900,
                metadata={"channel_type": "feishu", "channel_id": "ou_xxx"},
            )
            assert session.ttl_seconds == 900
            assert dict(session.metadata)["channel_type"] == "feishu"

    @pytest.mark.asyncio
    async def test_get_or_create_returns_existing(self):
        from src.conversation.session_manager import SessionManager
        from src.conversation.session_store import FileSessionStore

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = FileSessionStore(Path(tmp))
            manager = SessionManager(store=store)

            s1 = await manager.get_or_create("t1", "demo", "w1")
            s2 = await manager.get_or_create("t1", "demo", "w1")

            # Same session returned
            assert s1.session_id == s2.session_id

    @pytest.mark.asyncio
    async def test_cleanup_expired(self):
        from src.conversation.session_manager import SessionManager
        from src.conversation.session_store import FileSessionStore

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = FileSessionStore(Path(tmp))
            # TTL of 0 means everything expires immediately
            manager = SessionManager(store=store, ttl_seconds=0)

            session = await manager.get_or_create("t1", "demo", "w1")
            await manager.save(session)

            assert manager.cached_session_count == 1

            cleaned = await manager.cleanup_expired()
            assert cleaned == 1
            assert manager.cached_session_count == 0

    @pytest.mark.asyncio
    async def test_count_active_sessions_respects_worker_and_ttl(self):
        from src.conversation.session_manager import SessionManager
        from src.conversation.session_store import FileSessionStore

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = FileSessionStore(Path(tmp))
            manager = SessionManager(store=store, ttl_seconds=3600)

            session1 = await manager.get_or_create(
                "t1", "demo", "svc-1", ttl_seconds=900,
            )
            session2 = await manager.get_or_create(
                "t2", "demo", "svc-1", ttl_seconds=900,
            )
            session3 = await manager.get_or_create(
                "t3", "demo", "other-worker", ttl_seconds=900,
            )
            await manager.save(session1)
            await manager.save(session2)
            await manager.save(session3)

            count = await manager.count_active_sessions(
                tenant_id="demo",
                worker_id="svc-1",
            )
            assert count == 2

            count_excluding = await manager.count_active_sessions(
                tenant_id="demo",
                worker_id="svc-1",
                exclude_thread_id="t1",
            )
            assert count_excluding == 1

    @pytest.mark.asyncio
    async def test_main_session_not_counted_or_expired(self):
        from src.conversation.session_manager import SessionManager
        from src.conversation.session_store import FileSessionStore

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = FileSessionStore(Path(tmp))
            manager = SessionManager(store=store, ttl_seconds=0)

            main_session = await manager.get_or_create(
                "main:w1",
                "demo",
                "w1",
                session_type="main",
                main_session_key="main:w1",
            )
            await manager.save(main_session)

            active = await manager.count_active_sessions(
                tenant_id="demo",
                worker_id="w1",
            )
            cleaned = await manager.cleanup_expired()

            assert active == 0
            assert cleaned == 0

    def test_service_queue_tracks_position_and_dequeue(self):
        from src.conversation.session_manager import SessionManager

        class _DummyStore:
            async def load(self, session_id):
                return None

            async def save(self, session):
                return None

            async def delete(self, session_id):
                return None

            async def find_by_thread(self, thread_id):
                return None

            async def list_sessions(self, tenant_id, worker_id):
                return ()

        manager = SessionManager(store=_DummyStore())

        pos1 = manager.enqueue_service_session(
            tenant_id="demo", worker_id="svc-1", thread_id="t1",
        )
        pos2 = manager.enqueue_service_session(
            tenant_id="demo", worker_id="svc-1", thread_id="t2",
        )
        assert pos1 == 1
        assert pos2 == 2
        assert manager.get_service_queue_position(
            tenant_id="demo", worker_id="svc-1", thread_id="t2",
        ) == 2

        manager.dequeue_service_session(
            tenant_id="demo", worker_id="svc-1", thread_id="t1",
        )
        assert manager.get_service_queue_position(
            tenant_id="demo", worker_id="svc-1", thread_id="t2",
        ) == 1

    def test_stale_service_queue_entries_are_pruned(self):
        from src.conversation.session_manager import SessionManager

        class _DummyStore:
            async def load(self, session_id):
                return None

            async def save(self, session):
                return None

            async def delete(self, session_id):
                return None

            async def find_by_thread(self, thread_id):
                return None

            async def list_sessions(self, tenant_id, worker_id):
                return ()

        manager = SessionManager(store=_DummyStore())
        manager.enqueue_service_session(
            tenant_id="demo",
            worker_id="svc-1",
            thread_id="stale-thread",
            ttl_seconds=1,
        )
        manager._service_wait_queues[("demo", "svc-1")][0].queued_at = "2000-01-01T00:00:00+00:00"

        removed = manager.cleanup_stale_service_queues()

        assert removed == 1
        assert manager.get_service_queue_position(
            tenant_id="demo",
            worker_id="svc-1",
            thread_id="stale-thread",
        ) is None

    @pytest.mark.asyncio
    async def test_failed_task_notification(self):
        from src.conversation.session_manager import SessionManager
        from src.conversation.session_store import FileSessionStore

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = FileSessionStore(Path(tmp))
            manager = SessionManager(store=store)

            session = await manager.get_or_create("t1", "demo", "w1")

            # Record a task failure
            manager.record_task_failure(
                session_id=session.session_id,
                error_message="Task xyz failed: timeout",
            )

            # On next get_or_create, failure injected as system message
            session2 = await manager.get_or_create("t1", "demo", "w1")
            system_msgs = [
                m for m in session2.messages if m.role == "system"
            ]
            assert len(system_msgs) == 1
            assert "Task xyz failed" in system_msgs[0].content


class TestFileSessionStore:
    """Tests for FileSessionStore persistence."""

    @pytest.mark.asyncio
    async def test_save_and_load(self):
        from src.conversation.session_store import FileSessionStore

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = FileSessionStore(Path(tmp))

            session = ConversationSession(
                session_id="s1",
                thread_id="t1",
                tenant_id="demo",
                worker_id="w1",
            )
            await store.save(session)

            loaded = await store.find_by_thread("t1")
            assert loaded is not None
            assert loaded.session_id == "s1"

    @pytest.mark.asyncio
    async def test_delete(self):
        from src.conversation.session_store import FileSessionStore

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = FileSessionStore(Path(tmp))

            session = ConversationSession(
                session_id="s1",
                thread_id="t1",
                tenant_id="demo",
                worker_id="w1",
            )
            await store.save(session)
            await store.delete("s1")

            loaded = await store.find_by_thread("t1")
            assert loaded is None

    @pytest.mark.asyncio
    async def test_find_by_thread_not_found(self):
        from src.conversation.session_store import FileSessionStore

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = FileSessionStore(Path(tmp))
            result = await store.find_by_thread("nonexistent")
            assert result is None

    @pytest.mark.asyncio
    async def test_list_sessions_by_worker(self):
        from src.conversation.session_store import FileSessionStore

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = FileSessionStore(Path(tmp))

            s1 = ConversationSession(
                session_id="s1",
                thread_id="t1",
                tenant_id="demo",
                worker_id="svc-1",
            )
            s2 = ConversationSession(
                session_id="s2",
                thread_id="t2",
                tenant_id="demo",
                worker_id="svc-1",
            )
            s3 = ConversationSession(
                session_id="s3",
                thread_id="t3",
                tenant_id="demo",
                worker_id="other",
            )
            await store.save(s1)
            await store.save(s2)
            await store.save(s3)

            sessions = await store.list_sessions("demo", "svc-1")
            assert {session.session_id for session in sessions} == {"s1", "s2"}
