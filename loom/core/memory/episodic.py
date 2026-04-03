"""
Episodic Memory — ordered log of everything that happened in a session.

Entries are written after every tool call by TraceMiddleware's callback.
At session end they are compressed into SemanticMemory via the LLM.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any

import aiosqlite


@dataclass
class EpisodicEntry:
    session_id: str
    event_type: str          # "tool_call" | "tool_result" | "message" | "system"
    content: str             # Human-readable description of what happened
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class EpisodicMemory:
    """Read/write access to the episodic_entries table."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def write(self, entry: EpisodicEntry) -> None:
        await self._db.execute(
            """
            INSERT INTO episodic_entries
                (id, session_id, event_type, content, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                entry.id,
                entry.session_id,
                entry.event_type,
                entry.content,
                json.dumps(entry.metadata, ensure_ascii=False),
                entry.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def read_session(self, session_id: str) -> list[EpisodicEntry]:
        cursor = await self._db.execute(
            "SELECT id, session_id, event_type, content, metadata, created_at "
            "FROM episodic_entries WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [
            EpisodicEntry(
                id=r[0],
                session_id=r[1],
                event_type=r[2],
                content=r[3],
                metadata=json.loads(r[4]),
                created_at=datetime.fromisoformat(r[5]),
            )
            for r in rows
        ]

    async def count_session(self, session_id: str) -> int:
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM episodic_entries WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def delete_session(self, session_id: str) -> int:
        """Delete all episodic entries for *session_id*. Returns count deleted."""
        cursor = await self._db.execute(
            "DELETE FROM episodic_entries WHERE session_id = ?",
            (session_id,),
        )
        await self._db.commit()
        return cursor.rowcount or 0
