"""
Semantic Memory — long-term store of facts distilled from episodic events.

Facts are written by the session compressor at the end of each session.
Each fact has a confidence score and can be updated or queried by key.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any

import aiosqlite


@dataclass
class SemanticEntry:
    key: str                # Unique identifier for this fact
    value: str              # The actual fact in natural language
    confidence: float = 1.0
    source: str | None = None   # e.g. "session:abc123" or "manual"
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class SemanticMemory:
    """Read/write access to the semantic_entries table."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def upsert(self, entry: SemanticEntry) -> None:
        """Insert or update a fact by key."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """
            INSERT INTO semantic_entries
                (id, key, value, confidence, source, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value      = excluded.value,
                confidence = excluded.confidence,
                source     = excluded.source,
                metadata   = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                entry.id,
                entry.key,
                entry.value,
                entry.confidence,
                entry.source,
                json.dumps(entry.metadata, ensure_ascii=False),
                entry.created_at.isoformat(),
                now,
            ),
        )
        await self._db.commit()

    async def get(self, key: str) -> SemanticEntry | None:
        cursor = await self._db.execute(
            "SELECT id, key, value, confidence, source, metadata, created_at, updated_at "
            "FROM semantic_entries WHERE key = ?",
            (key,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return SemanticEntry(
            id=row[0], key=row[1], value=row[2], confidence=row[3],
            source=row[4], metadata=json.loads(row[5]),
            created_at=datetime.fromisoformat(row[6]),
            updated_at=datetime.fromisoformat(row[7]),
        )

    async def list_recent(self, limit: int = 20) -> list[SemanticEntry]:
        cursor = await self._db.execute(
            "SELECT id, key, value, confidence, source, metadata, created_at, updated_at "
            "FROM semantic_entries ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            SemanticEntry(
                id=r[0], key=r[1], value=r[2], confidence=r[3],
                source=r[4], metadata=json.loads(r[5]),
                created_at=datetime.fromisoformat(r[6]),
                updated_at=datetime.fromisoformat(r[7]),
            )
            for r in rows
        ]

    async def search(self, query: str, limit: int = 10) -> list[SemanticEntry]:
        """Simple substring search — full-text search can be added later."""
        cursor = await self._db.execute(
            "SELECT id, key, value, confidence, source, metadata, created_at, updated_at "
            "FROM semantic_entries WHERE value LIKE ? ORDER BY confidence DESC LIMIT ?",
            (f"%{query}%", limit),
        )
        rows = await cursor.fetchall()
        return [
            SemanticEntry(
                id=r[0], key=r[1], value=r[2], confidence=r[3],
                source=r[4], metadata=json.loads(r[5]),
                created_at=datetime.fromisoformat(r[6]),
                updated_at=datetime.fromisoformat(r[7]),
            )
            for r in rows
        ]
