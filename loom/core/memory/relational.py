"""
Relational Memory — (subject, predicate, object) triple store.

Stores long-lived facts about relationships: user preferences, project
constraints, collaboration style, and agent-observed behavioural patterns.

Each (subject, predicate) pair is unique — upserting replaces the object.
This makes it suitable for storing mutable preferences (e.g. a user's
preferred verbosity level) without accumulating stale entries.

Examples
--------
    RelationalEntry(subject="user", predicate="prefers", object="concise responses")
    RelationalEntry(subject="project:loom", predicate="uses", object="SQLite WAL")
    RelationalEntry(subject="user", predicate="avoids", object="trailing summaries")
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any

import aiosqlite

from loom.core.memory.ontology import (
    DEFAULT_DOMAIN,
    DEFAULT_TEMPORAL,
    normalize_domain,
    normalize_temporal,
)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class RelationalEntry:
    """A single (subject, predicate, object) triple."""
    subject:   str                          # e.g. "user", "project:loom"
    predicate: str                          # e.g. "prefers", "uses", "avoids"
    object:    str                          # e.g. "concise responses"
    confidence: float = 1.0
    source:    str = "agent"
    metadata:  dict[str, Any] = field(default_factory=dict)
    id:        str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Memory Ontology v0.1 (issue #281)
    domain:    str = DEFAULT_DOMAIN
    temporal:  str = DEFAULT_TEMPORAL
    last_accessed_at: datetime | None = None

    def __post_init__(self) -> None:
        self.domain = normalize_domain(self.domain)
        self.temporal = normalize_temporal(self.temporal)

    def effective_confidence(self) -> float:
        """Time-decayed confidence using the (domain, temporal) half-life.

        Issue #299 — relational triples now decay across all sources, not
        just ``source='dreaming'``. See :mod:`loom.core.memory.lifecycle`
        for the half-life table.
        """
        from loom.core.memory.lifecycle import effective_confidence
        return effective_confidence(
            confidence=self.confidence,
            updated_at=self.updated_at,
            last_accessed_at=self.last_accessed_at,
            domain=self.domain,
            temporal=self.temporal,
        )


_SELECT_COLS = (
    "id, subject, predicate, object, confidence, source, metadata, "
    "created_at, updated_at, domain, temporal, last_accessed_at"
)


# ---------------------------------------------------------------------------
# Memory class
# ---------------------------------------------------------------------------

class RelationalMemory:
    """
    Read/write access to the ``relational_entries`` table.

    Uniqueness is enforced on (subject, predicate).
    Upserting with the same (subject, predicate) replaces the object.
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def upsert(self, entry: RelationalEntry) -> None:
        """Insert or update the (subject, predicate) entry."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """
            INSERT INTO relational_entries
                (id, subject, predicate, object, confidence, source, metadata,
                 created_at, updated_at, domain, temporal, last_accessed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject, predicate) DO UPDATE SET
                object     = excluded.object,
                confidence = excluded.confidence,
                source     = excluded.source,
                metadata   = excluded.metadata,
                updated_at = excluded.updated_at,
                domain     = excluded.domain,
                temporal   = excluded.temporal
            """,
            (
                entry.id,
                entry.subject,
                entry.predicate,
                entry.object,
                entry.confidence,
                entry.source,
                json.dumps(entry.metadata, ensure_ascii=False),
                entry.created_at.isoformat(),
                now,
                entry.domain,
                entry.temporal,
                entry.last_accessed_at.isoformat() if entry.last_accessed_at else None,
            ),
        )
        await self._db.commit()

    async def get(self, subject: str, predicate: str) -> RelationalEntry | None:
        """Return the entry for (subject, predicate), or None."""
        cursor = await self._db.execute(
            f"SELECT {_SELECT_COLS} FROM relational_entries "
            "WHERE subject = ? AND predicate = ?",
            (subject, predicate),
        )
        row = await cursor.fetchone()
        return self._row(row) if row else None

    async def query(
        self,
        subject: str | None = None,
        predicate: str | None = None,
    ) -> list[RelationalEntry]:
        """
        Return entries matching the given filters.

        Pass ``subject`` to get all predicates for a subject.
        Pass ``predicate`` to find all subjects with that predicate.
        Pass both for an exact lookup (same as ``get()`` but returns a list).
        Pass neither to return all entries.
        """
        base = f"SELECT {_SELECT_COLS} FROM relational_entries"
        params: tuple[str, ...]
        if subject and predicate:
            sql = f"{base} WHERE subject = ? AND predicate = ? ORDER BY updated_at DESC"
            params = (subject, predicate)
        elif subject:
            sql = f"{base} WHERE subject = ? ORDER BY updated_at DESC"
            params = (subject,)
        elif predicate:
            sql = f"{base} WHERE predicate = ? ORDER BY updated_at DESC"
            params = (predicate,)
        else:
            sql = f"{base} ORDER BY updated_at DESC"
            params = ()

        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        return [self._row(r) for r in rows]

    async def delete(self, subject: str, predicate: str) -> bool:
        """
        Delete the entry for (subject, predicate).
        Returns True if an entry was deleted.
        """
        cursor = await self._db.execute(
            "DELETE FROM relational_entries WHERE subject = ? AND predicate = ?",
            (subject, predicate),
        )
        await self._db.commit()
        return (cursor.rowcount or 0) > 0

    # ------------------------------------------------------------------

    @staticmethod
    def _row(row) -> RelationalEntry:
        return RelationalEntry(
            id=row[0],
            subject=row[1],
            predicate=row[2],
            object=row[3],
            confidence=row[4],
            source=row[5],
            metadata=json.loads(row[6]),
            created_at=datetime.fromisoformat(row[7]),
            updated_at=datetime.fromisoformat(row[8]),
            domain=row[9],
            temporal=row[10],
            last_accessed_at=datetime.fromisoformat(row[11]) if row[11] else None,
        )
