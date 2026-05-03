"""
Semantic Memory — long-term store of facts distilled from episodic events.

Facts are written by the session compressor at the end of each session.
Each fact has a confidence score and can be updated or queried by key.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

import aiosqlite

from loom.core.memory.ontology import (
    DEFAULT_DOMAIN,
    DEFAULT_TEMPORAL,
    normalize_domain,
    normalize_temporal,
)

if TYPE_CHECKING:
    from loom.core.memory.embeddings import EmbeddingProvider


# ---------------------------------------------------------------------------
# Trust-tier classification (Issue #43: Memory Governance)
# ---------------------------------------------------------------------------

TRUST_TIERS: dict[str, float] = {
    "user_explicit": 1.0,       # User directly told us (via manual input)
    "tool_verified": 0.9,       # Verified by tool execution result
    "agent_memorize": 0.85,     # Agent invoked memorize tool (PR #65 review)
    "session_compress": 0.8,    # LLM-compressed from episodic
    "agent_inferred": 0.7,      # Agent's own inference
    "dreaming": 0.6,            # Offline dream synthesis
    "skill_evolution": 0.65,    # Skill evolution hints
    "counter_factual": 0.75,    # Counter-factual reflections (tried & failed)
    "external": 0.5,            # From external sources (URLs, fetch, web)
    "unknown": 0.5,             # Unclassified
}


def classify_source(source: str | None) -> tuple[str, float]:
    """Classify a memory source string into a trust tier.

    Returns (tier_name, default_confidence).  The caller can use the
    default_confidence as a floor when the entry's explicit confidence
    is not set or to blend with the stored confidence.

    Classification uses prefix matching on the ``source`` field that is
    already written by every memory producer in the codebase::

        "session:<id>"           → session_compress
        "session:<id>:fact:<n>"  → session_compress
        "counter_factual:<id>"  → counter_factual
        "skill_eval:<id>"       → agent_inferred
        "skill_evolution"       → skill_evolution
        "dreaming"              → dreaming
        "manual" / "memorize"   → user_explicit
        None / ""               → unknown
    """
    if not source:
        return "unknown", TRUST_TIERS["unknown"]

    s = source.lower()

    if s in ("manual", "user"):
        return "user_explicit", TRUST_TIERS["user_explicit"]
    if s == "memorize":
        return "agent_memorize", TRUST_TIERS["agent_memorize"]
    if s.startswith("counter_factual"):
        return "counter_factual", TRUST_TIERS["counter_factual"]
    if s.startswith("skill_eval"):
        return "agent_inferred", TRUST_TIERS["agent_inferred"]
    if s == "skill_evolution":
        return "skill_evolution", TRUST_TIERS["skill_evolution"]
    if s == "dreaming":
        return "dreaming", TRUST_TIERS["dreaming"]
    if s.startswith("session:"):
        return "session_compress", TRUST_TIERS["session_compress"]
    if s.startswith("tool:"):
        return "tool_verified", TRUST_TIERS["tool_verified"]
    if s.startswith("fetch:") or s.startswith("web:"):
        return "external", TRUST_TIERS["external"]

    return "unknown", TRUST_TIERS["unknown"]


# Memory Lifecycle (issue #281 P2): half-life is now (domain × temporal)
# in loom/core/memory/lifecycle.py. The legacy ``_DEFAULT_HALF_LIFE_DAYS``
# constant + module-level ``_effective_confidence`` helper were removed —
# all callers go through ``MemoryLifecycle`` or the dataclass method below.


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
    # Memory Ontology v0.1 (issue #281)
    domain: str = DEFAULT_DOMAIN
    temporal: str = DEFAULT_TEMPORAL
    last_accessed_at: datetime | None = None

    def __post_init__(self) -> None:
        self.domain = normalize_domain(self.domain)
        self.temporal = normalize_temporal(self.temporal)

    def effective_confidence(self) -> float:
        """Time-decayed confidence using the (domain, temporal) half-life.

        See :mod:`loom.core.memory.lifecycle` for the half-life table.
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
    "id, key, value, confidence, source, metadata, created_at, updated_at, "
    "domain, temporal, last_accessed_at"
)


def _row_to_entry(row: tuple) -> SemanticEntry:
    """Build a SemanticEntry from a row that follows ``_SELECT_COLS`` ordering."""
    return SemanticEntry(
        id=row[0],
        key=row[1],
        value=row[2],
        confidence=row[3],
        source=row[4],
        metadata=json.loads(row[5]),
        created_at=datetime.fromisoformat(row[6]),
        updated_at=datetime.fromisoformat(row[7]),
        domain=row[8],
        temporal=row[9],
        last_accessed_at=datetime.fromisoformat(row[10]) if row[10] else None,
    )


class SemanticMemory:
    """Read/write access to the semantic_entries table."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        embedding_provider: "EmbeddingProvider | None" = None,
    ) -> None:
        self._db = db
        self._embeddings = embedding_provider
        self._health: Any = None  # Optional MemoryHealthTracker, set post-init

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
        logged and the entry's metadata is marked with
        ``embedding_status: "failed"`` so orphaned entries are auditable.
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
                (id, key, value, confidence, source, metadata, created_at, updated_at,
                 domain, temporal, last_accessed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value      = excluded.value,
                confidence = excluded.confidence,
                source     = excluded.source,
                metadata   = excluded.metadata,
                updated_at = excluded.updated_at,
                domain     = excluded.domain,
                temporal   = excluded.temporal
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
                entry.domain,
                entry.temporal,
                entry.last_accessed_at.isoformat() if entry.last_accessed_at else None,
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
            except Exception as exc:
                logger.warning(
                    "Embedding write failed for key=%r — memory saved "
                    "but semantic search will miss it: %s",
                    entry.key, exc,
                )
                if self._health:
                    self._health.record_failure("embedding_write", str(exc))
                # Mark the entry so we can audit orphaned embeddings later
                try:
                    meta = dict(entry.metadata)
                    meta["embedding_status"] = "failed"
                    await self._db.execute(
                        "UPDATE semantic_entries SET metadata = ? WHERE key = ?",
                        (json.dumps(meta, ensure_ascii=False), entry.key),
                    )
                    await self._db.commit()
                except Exception as ann_exc:
                    logger.debug(
                        "Failed to annotate embedding_status for key=%r: %s",
                        entry.key, ann_exc,
                    )
            else:
                if self._health:
                    self._health.record_success("embedding_write")

        return conflicted

    async def get(self, key: str) -> SemanticEntry | None:
        cursor = await self._db.execute(
            f"SELECT {_SELECT_COLS} FROM semantic_entries WHERE key = ?",
            (key,),
        )
        row = await cursor.fetchone()
        return _row_to_entry(row) if row else None

    async def list_recent(self, limit: int = 20) -> list[SemanticEntry]:
        cursor = await self._db.execute(
            f"SELECT {_SELECT_COLS} FROM semantic_entries "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [_row_to_entry(r) for r in rows]

    async def get_random(self, limit: int = 15) -> list[SemanticEntry]:
        """
        Return up to *limit* entries chosen at random from all semantic facts.

        Used by the dreaming cycle so each dream surfaces a varied, non-recency-
        biased sample of the knowledge base.  SQLite's ORDER BY RANDOM() is fine
        for typical Loom memory sizes (< 50k rows).
        """
        cursor = await self._db.execute(
            f"SELECT {_SELECT_COLS} FROM semantic_entries ORDER BY RANDOM() LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [_row_to_entry(r) for r in rows]

    async def list_with_embeddings(
        self, limit: int = 500
    ) -> list[tuple[SemanticEntry, list[float] | None]]:
        """
        Return entries together with their stored embedding vectors.
        Entries without a computed embedding return None for the vector.
        Used by MemorySearch for cosine-similarity ranking.
        """
        cursor = await self._db.execute(
            f"SELECT {_SELECT_COLS}, embedding FROM semantic_entries "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        result: list[tuple[SemanticEntry, list[float] | None]] = []
        for r in rows:
            entry = _row_to_entry(r[:11])
            vector: list[float] | None = json.loads(r[11]) if r[11] else None
            result.append((entry, vector))
        return result

    async def search(self, query: str, limit: int = 10) -> list[SemanticEntry]:
        """Simple substring search — full-text search can be added later."""
        cursor = await self._db.execute(
            f"SELECT {_SELECT_COLS} FROM semantic_entries "
            "WHERE value LIKE ? ORDER BY confidence DESC LIMIT ?",
            (f"%{query}%", limit),
        )
        rows = await cursor.fetchall()
        return [_row_to_entry(r) for r in rows]

    async def list_by_prefix(self, prefix: str, limit: int = 10) -> list[SemanticEntry]:
        """Return entries whose key starts with *prefix*, newest first.

        Used by skill evolution hints to query entries matching
        ``skill:<name>:evolution_hint:*`` without full-text search overhead.
        """
        cursor = await self._db.execute(
            f"SELECT {_SELECT_COLS} FROM semantic_entries "
            "WHERE key LIKE ? ORDER BY updated_at DESC LIMIT ?",
            (f"{prefix}%", limit),
        )
        rows = await cursor.fetchall()
        return [_row_to_entry(r) for r in rows]

    async def prune_decayed(
        self,
        threshold: float = 0.1,
        dry_run: bool = False,
    ) -> dict:
        """Run one decay+demote cycle on the semantic table only.

        Delegates to :class:`MemoryLifecycle` — half-life is determined per
        ``(domain, temporal)`` (see ``lifecycle._HALF_LIFE_TABLE``) rather
        than the legacy single-90d value. Two-stage transition: rows with
        ``temporal='recent'`` whose effective_confidence has fallen below
        ``threshold`` are demoted to ``archived`` (preserved for second-
        chance recall); rows already ``archived`` and still below threshold
        are deleted.

        Returns the same dict shape callers expect (``examined`` / ``pruned``
        / ``retained``), where ``pruned`` totals archived + deleted so the
        ``memory_prune`` tool's output stays meaningful.
        """
        from loom.core.memory.lifecycle import MemoryLifecycle

        cycle = MemoryLifecycle(self._db, threshold=threshold)
        examined, archived, deleted = await cycle.run_for_table(
            "semantic_entries", dry_run=dry_run,
        )
        pruned = archived + deleted

        return {
            "examined": examined,
            "pruned": pruned,
            "archived": archived,
            "deleted": deleted,
            "retained": examined - pruned,
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

    async def mark_accessed(self, keys: list[str]) -> None:
        """Bump ``last_accessed_at`` for the given keys (Memory Ontology v0.1).

        Called by :class:`MemorySearch` after a successful recall so the
        decay cycle can distinguish facts that are still in active use from
        those drifting toward archived state. Silent no-op for empty keys
        list — recall hot path stays cheap.
        """
        if not keys:
            return
        now = datetime.now(UTC).isoformat()
        placeholders = ",".join("?" * len(keys))
        await self._db.execute(
            f"UPDATE semantic_entries SET last_accessed_at = ? "
            f"WHERE key IN ({placeholders})",
            (now, *keys),
        )
        await self._db.commit()

