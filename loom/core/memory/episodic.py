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
    compressed_at: datetime | None = None


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

    async def read_session(
        self,
        session_id: str,
        *,
        uncompressed_only: bool = False,
    ) -> list[EpisodicEntry]:
        """Read episodic entries for *session_id*.

        When ``uncompressed_only`` is True, excludes entries already folded
        into semantic memory (compressed_at IS NOT NULL). The compression
        path uses this to avoid re-processing; callers that want to replay
        the full session trace should leave it False (default).
        """
        sql = (
            "SELECT id, session_id, event_type, content, metadata, "
            "created_at, compressed_at "
            "FROM episodic_entries WHERE session_id = ?"
        )
        if uncompressed_only:
            sql += " AND compressed_at IS NULL"
        sql += " ORDER BY created_at"
        cursor = await self._db.execute(sql, (session_id,))
        rows = await cursor.fetchall()
        return [
            EpisodicEntry(
                id=r[0],
                session_id=r[1],
                event_type=r[2],
                content=r[3],
                metadata=json.loads(r[4]),
                created_at=datetime.fromisoformat(r[5]),
                compressed_at=datetime.fromisoformat(r[6]) if r[6] else None,
            )
            for r in rows
        ]

    async def count_session(
        self,
        session_id: str,
        *,
        uncompressed_only: bool = False,
    ) -> int:
        """Count episodic entries for *session_id*.

        The compression trigger passes ``uncompressed_only=True`` so that
        re-compressed rows don't keep the threshold permanently satisfied.
        """
        sql = "SELECT COUNT(*) FROM episodic_entries WHERE session_id = ?"
        if uncompressed_only:
            sql += " AND compressed_at IS NULL"
        cursor = await self._db.execute(sql, (session_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def mark_compressed(
        self,
        entry_ids: list[str],
        *,
        compressed_at: datetime | None = None,
    ) -> int:
        """Mark the given entries as compressed (soft-delete).

        Preserves the original rows for audit and potential recovery; the
        TTL prune in ``MemoryGovernor._prune_episodic_ttl`` eventually hard-
        deletes them based on ``created_at``.
        """
        if not entry_ids:
            return 0
        stamp = (compressed_at or datetime.now(UTC)).isoformat()
        placeholders = ",".join("?" * len(entry_ids))
        cursor = await self._db.execute(
            f"UPDATE episodic_entries SET compressed_at = ? "
            f"WHERE id IN ({placeholders}) AND compressed_at IS NULL",
            (stamp, *entry_ids),
        )
        await self._db.commit()
        return cursor.rowcount or 0

    async def delete_session(self, session_id: str) -> int:
        """Hard-delete all episodic entries for *session_id*. Returns count deleted.

        No longer used by the compression path (which now soft-deletes via
        ``mark_compressed``). Retained for user-initiated purge and tests.
        """
        cursor = await self._db.execute(
            "DELETE FROM episodic_entries WHERE session_id = ?",
            (session_id,),
        )
        await self._db.commit()
        return cursor.rowcount or 0
