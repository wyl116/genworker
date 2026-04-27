"""SQLite FTS5 index for raw conversation session search."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from src.common.logger import get_logger
from src.conversation.models import ConversationSession

logger = get_logger()


@dataclass(frozen=True)
class SearchHit:
    session_id: str
    thread_id: str
    role: str
    content: str
    snippet: str
    created_at: str
    rank: float


@dataclass(frozen=True)
class SearchResult:
    hits: tuple[SearchHit, ...]
    total_count: int
    query: str


class SessionSearchIndex:
    """Maintain a tenant-scoped FTS5 index over raw session messages."""

    def __init__(self, db_path: str) -> None:
        self._db_path = Path(db_path)

    async def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS session_messages (
                    message_key TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata TEXT DEFAULT '{}'
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS session_messages_fts USING fts5(
                    content,
                    content='session_messages',
                    content_rowid='rowid',
                    tokenize='unicode61'
                );
                CREATE TRIGGER IF NOT EXISTS session_messages_ai AFTER INSERT ON session_messages BEGIN
                    INSERT INTO session_messages_fts(rowid, content) VALUES (new.rowid, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS session_messages_ad AFTER DELETE ON session_messages BEGIN
                    INSERT INTO session_messages_fts(session_messages_fts, rowid, content) VALUES('delete', old.rowid, old.content);
                END;
                CREATE TRIGGER IF NOT EXISTS session_messages_au AFTER UPDATE ON session_messages BEGIN
                    INSERT INTO session_messages_fts(session_messages_fts, rowid, content) VALUES('delete', old.rowid, old.content);
                    INSERT INTO session_messages_fts(rowid, content) VALUES (new.rowid, new.content);
                END;
                CREATE INDEX IF NOT EXISTS idx_session_messages_scope
                ON session_messages(tenant_id, worker_id, created_at);
                """
            )

    async def upsert_session_messages(self, session: ConversationSession) -> int:
        inserted = 0
        with self._connect() as conn:
            for message in session.messages:
                message_key = _build_message_key(
                    session.session_id,
                    message.role,
                    message.content,
                    message.timestamp,
                )
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO session_messages (
                        message_key, session_id, thread_id, tenant_id, worker_id,
                        role, content, created_at, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_key,
                        session.session_id,
                        session.thread_id,
                        session.tenant_id,
                        session.worker_id,
                        message.role,
                        message.content,
                        message.timestamp,
                        json.dumps(
                            {
                                "skill_id": message.skill_id or "",
                                "spawned_task_id": message.spawned_task_id or "",
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    async def search(
        self,
        *,
        query: str,
        tenant_id: str,
        worker_id: str,
        date_start: str = "",
        date_end: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> SearchResult:
        safe_limit = min(max(1, int(limit)), 50)
        safe_offset = max(0, int(offset))
        conditions = [
            "session_messages_fts MATCH ?",
            "m.tenant_id = ?",
        ]
        params: list[object] = [query, tenant_id]
        if worker_id:
            conditions.append("m.worker_id = ?")
            params.append(worker_id)
        if date_start:
            conditions.append("m.created_at >= ?")
            params.append(date_start)
        if date_end:
            conditions.append("m.created_at <= ?")
            params.append(date_end)

        where_clause = " AND ".join(conditions)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    m.session_id,
                    m.thread_id,
                    m.role,
                    m.content,
                    snippet(session_messages_fts, 0, '[', ']', '...', 16) AS snippet,
                    m.created_at,
                    bm25(session_messages_fts) AS rank
                FROM session_messages_fts
                JOIN session_messages AS m ON m.rowid = session_messages_fts.rowid
                WHERE {where_clause}
                ORDER BY rank, m.created_at DESC
                LIMIT ? OFFSET ?
                """,
                [*params, safe_limit, safe_offset],
            ).fetchall()
            total = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM session_messages_fts
                JOIN session_messages AS m ON m.rowid = session_messages_fts.rowid
                WHERE {where_clause}
                """,
                params,
            ).fetchone()
        hits = tuple(
            SearchHit(
                session_id=str(row[0]),
                thread_id=str(row[1]),
                role=str(row[2]),
                content=str(row[3]),
                snippet=str(row[4]),
                created_at=str(row[5]),
                rank=float(row[6]),
            )
            for row in rows
        )
        return SearchResult(
            hits=hits,
            total_count=int(total[0]) if total else 0,
            query=query,
        )

    async def delete_session(self, session_id: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM session_messages WHERE session_id = ?",
                (session_id,),
            )
            return int(cursor.rowcount)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn


def _build_message_key(
    session_id: str,
    role: str,
    content: str,
    timestamp: str,
) -> str:
    raw = f"{session_id}:{role}:{content}:{timestamp}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]

