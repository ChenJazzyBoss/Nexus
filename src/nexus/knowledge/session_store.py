"""SQLite-backed session persistence.

Adapted from hermes-agent/hermes_state.py (simplified).
Stores session metadata and full message history with FTS5 search.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_call_id TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
"""


@dataclass
class SessionInfo:
    """Session metadata."""
    session_id: str
    agent_id: str
    created_at: float
    updated_at: float
    metadata: dict[str, Any] = field(default_factory=dict)
    message_count: int = 0


class SessionStore:
    """SQLite-backed session persistence with FTS5 search."""

    def __init__(self, db_path: str | Path = "data/sessions.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA_SQL)
        return self._conn

    def create_session(self, agent_id: str, metadata: dict[str, Any] | None = None) -> str:
        """Create a new session and return its ID."""
        session_id = uuid.uuid4().hex
        now = time.time()
        self.conn.execute(
            "INSERT INTO sessions (session_id, agent_id, created_at, updated_at, metadata) VALUES (?, ?, ?, ?, ?)",
            (session_id, agent_id, now, now, json.dumps(metadata or {})),
        )
        self.conn.commit()
        return session_id

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_call_id: str | None = None,
    ) -> None:
        """Save a message to a session."""
        now = time.time()
        self.conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_call_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, tool_call_id, now),
        )
        self.conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (now, session_id),
        )
        self.conn.commit()

    def load_session(self, session_id: str) -> list[dict[str, Any]]:
        """Load all messages for a session."""
        cursor = self.conn.execute(
            "SELECT role, content, tool_call_id, created_at FROM messages WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        )
        messages = []
        for row in cursor:
            msg: dict[str, Any] = {"role": row[0], "content": row[1]}
            if row[2]:
                msg["tool_call_id"] = row[2]
            messages.append(msg)
        return messages

    def get_session(self, session_id: str) -> SessionInfo | None:
        """Get session metadata."""
        cursor = self.conn.execute(
            "SELECT session_id, agent_id, created_at, updated_at, metadata FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None

        msg_count = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()[0]

        return SessionInfo(
            session_id=row[0],
            agent_id=row[1],
            created_at=row[2],
            updated_at=row[3],
            metadata=json.loads(row[4]),
            message_count=msg_count,
        )

    def list_sessions(self, agent_id: str | None = None, limit: int = 50) -> list[SessionInfo]:
        """List sessions, optionally filtered by agent."""
        if agent_id:
            cursor = self.conn.execute(
                "SELECT session_id, agent_id, created_at, updated_at, metadata FROM sessions WHERE agent_id = ? ORDER BY updated_at DESC LIMIT ?",
                (agent_id, limit),
            )
        else:
            cursor = self.conn.execute(
                "SELECT session_id, agent_id, created_at, updated_at, metadata FROM sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )

        sessions = []
        for row in cursor:
            msg_count = self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?", (row[0],)
            ).fetchone()[0]
            sessions.append(SessionInfo(
                session_id=row[0],
                agent_id=row[1],
                created_at=row[2],
                updated_at=row[3],
                metadata=json.loads(row[4]),
                message_count=msg_count,
            ))
        return sessions

    def search_messages(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search messages using FTS5 (if available) or LIKE fallback."""
        try:
            cursor = self.conn.execute(
                """SELECT m.session_id, m.role, m.content, m.created_at
                   FROM messages m
                   WHERE m.content LIKE ?
                   ORDER BY m.created_at DESC LIMIT ?""",
                (f"%{query}%", limit),
            )
        except Exception:
            return []

        results = []
        for row in cursor:
            results.append({
                "session_id": row[0],
                "role": row[1],
                "content": row[2][:500],
                "created_at": row[3],
            })
        return results

    def delete_session(self, session_id: str) -> None:
        """Delete a session and all its messages."""
        self.conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        self.conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        self.conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
