"""
Conversation data models - all frozen dataclasses for immutability.

Defines:
- ChatMessage: a single message in a conversation
- ConversationSession: multi-turn conversation context
"""
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Optional


def _now_iso() -> str:
    """ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ChatMessage:
    """A single message in a conversation."""
    role: str                                    # "user" | "assistant" | "system"
    content: str
    timestamp: str = field(default_factory=_now_iso)
    skill_id: Optional[str] = None               # skill matched for this turn
    spawned_task_id: Optional[str] = None         # task spawned during this turn


@dataclass(frozen=True)
class ConversationSession:
    """
    Multi-turn conversation session (immutable).

    All mutations return a new object via dataclasses.replace().
    """
    session_id: str
    thread_id: str
    tenant_id: str
    worker_id: str
    messages: tuple[ChatMessage, ...] = ()
    spawned_tasks: tuple[str, ...] = ()
    created_at: str = field(default_factory=_now_iso)
    last_active_at: str = field(default_factory=_now_iso)
    ttl_seconds: int = 0
    metadata: tuple[tuple[str, str], ...] = ()
    session_type: str = "thread"
    main_session_key: Optional[str] = None
    inbox_cursor: Optional[str] = None
    last_heartbeat_at: Optional[str] = None
    open_concerns: tuple[str, ...] = ()
    task_refs: tuple[str, ...] = ()

    def append_message(self, message: ChatMessage) -> "ConversationSession":
        """Return a new session with the message appended."""
        return replace(
            self,
            messages=self.messages + (message,),
            last_active_at=message.timestamp,
        )

    def add_spawned_task(self, task_id: str) -> "ConversationSession":
        """Return a new session with the task_id recorded."""
        return replace(
            self,
            spawned_tasks=self.spawned_tasks + (task_id,),
        )

    def to_dict(self) -> dict:
        """Serialize to dict for JSON storage."""
        return {
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "tenant_id": self.tenant_id,
            "worker_id": self.worker_id,
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "timestamp": m.timestamp,
                    "skill_id": m.skill_id,
                    "spawned_task_id": m.spawned_task_id,
                }
                for m in self.messages
            ],
            "spawned_tasks": list(self.spawned_tasks),
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "ttl_seconds": self.ttl_seconds,
            "metadata": [list(pair) for pair in self.metadata],
            "session_type": self.session_type,
            "main_session_key": self.main_session_key,
            "inbox_cursor": self.inbox_cursor,
            "last_heartbeat_at": self.last_heartbeat_at,
            "open_concerns": list(self.open_concerns),
            "task_refs": list(self.task_refs),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ConversationSession":
        """Deserialize from dict."""
        messages = tuple(
            ChatMessage(
                role=m.get("role", "user"),
                content=m.get("content", ""),
                timestamp=m.get("timestamp", ""),
                skill_id=m.get("skill_id"),
                spawned_task_id=m.get("spawned_task_id"),
            )
            for m in data.get("messages", [])
        )
        metadata = tuple(
            (pair[0], pair[1])
            for pair in data.get("metadata", [])
            if len(pair) >= 2
        )
        return cls(
            session_id=data.get("session_id", ""),
            thread_id=data.get("thread_id", ""),
            tenant_id=data.get("tenant_id", ""),
            worker_id=data.get("worker_id", ""),
            messages=messages,
            spawned_tasks=tuple(data.get("spawned_tasks", [])),
            created_at=data.get("created_at", ""),
            last_active_at=data.get("last_active_at", ""),
            ttl_seconds=int(data.get("ttl_seconds", 0) or 0),
            metadata=metadata,
            session_type=data.get("session_type", "thread"),
            main_session_key=data.get("main_session_key"),
            inbox_cursor=data.get("inbox_cursor"),
            last_heartbeat_at=data.get("last_heartbeat_at"),
            open_concerns=tuple(data.get("open_concerns", [])),
            task_refs=tuple(data.get("task_refs", [])),
        )
