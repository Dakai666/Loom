"""
Semantic Memory — long-term store of facts distilled from episodic events.

Facts are written by the session compressor at the end of each session.
Each fact has a confidence score and can be updated or queried by key.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from loom.core.memory.embeddings import EmbeddingProvider


_DEFAULT_HALF_LIFE_DAYS = 90.0  # confidence halves after this many days of no update


def _effective_confidence(confidence: float, updated_at: datetime,
                           half_life_days: float = _DEFAULT_HALF_LIFE_DAYS) -> float:
    """Exponential decay: confidence * 2^(-days_since_update / half_life).
    Returns at least 0.01 so a stale entry is never completely invisible."""
    days = (datetime.now(UTC) - updated_at).total_seconds() / 86400.0
    decayed = confidence * math.pow(2.0, -days / half_life_days)
    return max(0.01, round(decayed, 4))


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

    def effective_confidence(self, half_life_days: float = _DEFAULT_HALF_LIFE_DAYS) -> float:
        """Time-decayed confidence score."""
        return _effective_confidence(self.confidence, self.updated_at, half_life_days)


class SemanticMemory:
    """Read/write access to the semantic_entries table."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        embedding_provider: "EmbeddingProvider | None" = None,
    ) -> None:
        self._db = db
        self._embeddings = embedding_provider

    @property
    def has_embeddings(self) -> bool:
        """True if an embedding provider is configured."""
        return self._embeddings is not None

    async def upsert(self, entry: SemanticEntry) -> bool:
        """
        Insert or update a fact by key.  Returns True if an existing value
        was overwritten (conflict), False for a clean insert.

        On conflict the previous value is appended to metadata["history"]
        (capped at 3 entries) so overwrites are traceable.

        If an embedding provider is configured, computes and persists the
        vector for this entry after the upsert.  Embedding failures are
        silently swallowed so a network error never blocks a memory write.
        """
        now = datetime.now(UTC).isoformat()

        # Check for an existing entry to record conflict provenance
        existing = await self.get(entry.key)
        conflicted = False
        merged_metadata = dict(entry.metadata)

        if existing is not None and existing.value != entry.value:
            conflicted = True
            # Preserve history of overwritten values (last 3)
            history: list = existing.metadata.get("history", [])
            history.append({
                "value": existing.value[:200],
                "source": existing.source,
                "updated_at": existing.updated_at.isoformat() if existing.updated_at else "",
            })
            merged_metadata["history"] = history[-3:]

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
                json.dumps(merged_metadata, ensure_ascii=False),
                entry.created_at.isoformat(),
                now,
            ),
        )
        await self._db.commit()

        if self._embeddings is not None:
            try:
                text = f"{entry.key} {entry.value}"
                vectors = await self._embeddings.embed([text])
                if vectors:
                    await self._db.execute(
                        "UPDATE semantic_entries SET embedding = ? WHERE key = ?",
                        (json.dumps(vectors[0]), entry.key),
                    )
                    await self._db.commit()
            except Exception:
                pass  # Embedding failure must never block the memory write

        return conflicted

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

    async def get_random(self, limit: int = 15) -> list[SemanticEntry]:
        """
        Return up to *limit* entries chosen at random from all semantic facts.

        Used by the dreaming cycle so each dream surfaces a varied, non-recency-
        biased sample of the knowledge base.  SQLite's ORDER BY RANDOM() is fine
        for typical Loom memory sizes (< 50k rows).
        """
        cursor = await self._db.execute(
            "SELECT id, key, value, confidence, source, metadata, created_at, updated_at "
            "FROM semantic_entries ORDER BY RANDOM() LIMIT ?",
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

    async def list_with_embeddings(
        self, limit: int = 500
    ) -> list[tuple[SemanticEntry, list[float] | None]]:
        """
        Return entries together with their stored embedding vectors.
        Entries without a computed embedding return None for the vector.
        Used by MemorySearch for cosine-similarity ranking.
        """
        cursor = await self._db.execute(
            "SELECT id, key, value, confidence, source, metadata, "
            "created_at, updated_at, embedding "
            "FROM semantic_entries ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        result: list[tuple[SemanticEntry, list[float] | None]] = []
        for r in rows:
            entry = SemanticEntry(
                id=r[0], key=r[1], value=r[2], confidence=r[3],
                source=r[4], metadata=json.loads(r[5]),
                created_at=datetime.fromisoformat(r[6]),
                updated_at=datetime.fromisoformat(r[7]),
            )
            vector: list[float] | None = json.loads(r[8]) if r[8] else None
            result.append((entry, vector))
        return result

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

    async def prune_decayed(
        self,
        threshold: float = 0.1,
        dry_run: bool = False,
    ) -> dict:
        """
        Delete semantic entries whose effective_confidence has decayed below *threshold*.

        Effective confidence = stored_confidence × 2^(-days_since_update / half_life).
        A 90-day half-life means:
          - confidence=0.8 → drops below 0.1 after ~282 days of no update
          - confidence=1.0 → drops below 0.1 after ~299 days of no update

        Returns a dict: {examined, pruned, threshold, dry_run}
        If dry_run=True the query runs but nothing is deleted.
        """
        cursor = await self._db.execute(
            "SELECT key, confidence, updated_at FROM semantic_entries"
        )
        rows = await cursor.fetchall()

        to_prune: list[str] = []
        for key, confidence, updated_at_str in rows:
            updated_at = datetime.fromisoformat(updated_at_str)
            if _effective_confidence(confidence, updated_at) < threshold:
                to_prune.append(key)

        if not dry_run and to_prune:
            placeholders = ",".join("?" * len(to_prune))
            await self._db.execute(
                f"DELETE FROM semantic_entries WHERE key IN ({placeholders})",
                to_prune,
            )
            await self._db.commit()

        return {
            "examined": len(rows),
            "pruned": len(to_prune),
            "retained": len(rows) - len(to_prune),
            "threshold": threshold,
            "dry_run": dry_run,
        }

    async def count(self) -> int:
        """Return the total number of semantic entries in the database."""
        cursor = await self._db.execute("SELECT COUNT(*) FROM semantic_entries")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def count_compressed_sessions(self) -> int:
        """Return the number of distinct sessions that have been compressed into semantic memory."""
        cursor = await self._db.execute(
            "SELECT COUNT(DISTINCT substr(source, 1, instr(source, ':fact:') - 1)) "
            "FROM semantic_entries WHERE source LIKE 'session:%:fact:%'"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def delete(self, key: str) -> bool:
        """
        Delete a semantic entry by key.

        Returns True if an entry was actually deleted, False if no such key.
        Used by the memorize rollback function (Issue #42).
        """
        cursor = await self._db.execute(
            "DELETE FROM semantic_entries WHERE key = ?", (key,)
        )
        await self._db.commit()
        return (cursor.rowcount or 0) > 0

