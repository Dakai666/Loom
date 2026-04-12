"""
PalaceSearch — thin coordinator layer for cross-view search.

Uses existing MemorySearch (BM25 + FTS5 + embedding) for semantic and skills.
Adds simple SQL LIKE-based search for relational triples and session content.

Design principle: borrow + compose, never re-implement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import aiosqlite

from loom.core.memory.semantic import SemanticMemory
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.relational import RelationalMemory
from loom.core.memory.search import MemorySearch, MemorySearchResult
from loom.core.memory.session_log import SessionLog


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

SearchScope = Literal["semantic", "skill", "relational", "session", "all"]


@dataclass
class PalaceResult:
    """Unified result format across all memory types."""
    scope: SearchScope
    key: str           # e.g. fact key, subject, session_id, skill name
    value: str         # primary display text
    meta: str = ""     # secondary info line
    score: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# PalaceSearch
# ---------------------------------------------------------------------------

class PalaceSearch:
    """
    Thin coordinator that wraps existing memory stores for the palace UI.

    Semantic + Skills:  delegates to MemorySearch.recall() (BM25 + FTS5 + embedding)
    Relational:        SQL LIKE on subject/predicate/object
    Sessions:           SQL LIKE on session_log content
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._semantic = SemanticMemory(db)
        self._procedural = ProceduralMemory(db)
        self._relational = RelationalMemory(db)
        self._session_log = SessionLog(db)
        self._memory_search = MemorySearch(self._semantic, self._procedural)

    # ── Semantic / Skills (via MemorySearch) ─────────────────────────────

    async def search_semantic(
        self, query: str, limit: int = 20
    ) -> list[PalaceResult]:
        """BM25 + FTS5 search over semantic facts."""
        raw = await self._memory_search.recall(
            query, type="semantic", limit=limit
        )
        return [self._from_search_result(r) for r in raw]

    async def search_skills(
        self, query: str, limit: int = 20
    ) -> list[PalaceResult]:
        """BM25 + FTS5 search over skill genomes."""
        raw = await self._memory_search.recall(
            query, type="skill", limit=limit
        )
        return [self._from_search_result(r) for r in raw]

    async def search_all(
        self, query: str, limit: int = 20
    ) -> list[PalaceResult]:
        """Search semantic, skills, relational, and sessions simultaneously."""
        results: list[PalaceResult] = []
        results.extend(await self.search_semantic(query, limit))
        results.extend(await self.search_skills(query, limit))
        results.extend(await self.search_relational(query, limit))
        results.extend(await self.search_sessions(query, limit))
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]

    # ── Relational (SQL LIKE) ─────────────────────────────────────────────

    async def search_relational(
        self, query: str, limit: int = 20
    ) -> list[PalaceResult]:
        """
        Search relational triples by subject, predicate, or object content.
        No FTS5 needed — the triple structure is simple enough for LIKE.
        """
        like = f"%{query}%"
        cursor = await self._db.execute(
            """
            SELECT id, subject, predicate, object, confidence, source, metadata,
                   created_at, updated_at
            FROM relational_entries
            WHERE subject LIKE ? OR predicate LIKE ? OR object LIKE ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (like, like, like, limit),
        )
        rows = await cursor.fetchall()
        results: list[PalaceResult] = []
        for r in rows:
            subject, predicate, obj, conf = r[1], r[2], r[3], r[4]
            results.append(PalaceResult(
                scope="relational",
                key=subject,
                value=f"{subject}  ─{predicate}──▶  {obj}",
                meta=f"conf={conf:.2f}",
                score=0.0,
            ))
        return results

    async def list_relational_subjects(self) -> list[tuple[str, int]]:
        """Return all subjects with their triple count, ordered by count desc."""
        cursor = await self._db.execute(
            """
            SELECT subject, COUNT(*) as cnt
            FROM relational_entries
            GROUP BY subject
            ORDER BY cnt DESC
            LIMIT 100
            """
        )
        return [(r[0], r[1]) for r in await cursor.fetchall()]

    async def list_relational_by_subject(
        self, subject: str
    ) -> list[PalaceResult]:
        """List all triples for a given subject."""
        cursor = await self._db.execute(
            """
            SELECT id, subject, predicate, object, confidence, source,
                   created_at, updated_at
            FROM relational_entries
            WHERE subject = ?
            ORDER BY updated_at DESC
            """,
            (subject,),
        )
        rows = await cursor.fetchall()
        return [
            PalaceResult(
                scope="relational",
                key=r[1],
                value=f"{r[1]}  ─{r[2]}──▶  {r[3]}",
                meta=f"conf={r[4]:.2f} · updated={str(r[7])[:10]}",
                score=0.0,
                extra={"predicate": r[2], "object": r[3], "confidence": r[4]},
            )
            for r in rows
        ]

    # ── Sessions / Episodic (SQL LIKE) ───────────────────────────────────

    async def search_sessions(
        self, query: str, limit: int = 20
    ) -> list[PalaceResult]:
        """Search session_log content via SQL LIKE."""
        like = f"%{query}%"
        cursor = await self._db.execute(
            """
            SELECT DISTINCT sl.session_id, s.title, s.model,
                   s.started_at, s.last_active, s.turn_count
            FROM session_log sl
            JOIN sessions s ON s.session_id = sl.session_id
            WHERE sl.content LIKE ? AND sl.role = 'user'
            ORDER BY s.last_active DESC
            LIMIT ?
            """,
            (like, limit),
        )
        rows = await cursor.fetchall()
        return [
            PalaceResult(
                scope="session",
                key=r[0],
                value=r[1] or "(no title)",
                meta=f"model={r[2]} · {r[5]} turns · {r[4][:10]}",
                score=0.0,
                extra={"turn_count": r[5], "model": r[2]},
            )
            for r in rows
        ]

    # ── Stats helpers ────────────────────────────────────────────────────

    async def semantic_stats(self) -> dict[str, Any]:
        """Return counts and confidence distribution for semantic memory."""
        cursor = await self._db.execute(
            "SELECT COUNT(*), "
            "  SUM(CASE WHEN confidence > 0.7 THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN confidence BETWEEN 0.4 AND 0.7 THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN confidence <= 0.4 THEN 1 ELSE 0 END) "
            "FROM semantic_entries"
        )
        row = await cursor.fetchone()
        total, high, mid, low = (row or (0, 0, 0, 0))

        cursor2 = await self._db.execute(
            "SELECT COUNT(*) FROM semantic_entries "
            "WHERE updated_at > datetime('now', '-1 day')"
        )
        row2 = await cursor2.fetchone()
        today = row2[0] if row2 else 0

        return {
            "total": total,
            "high": high,
            "mid": mid,
            "low": low,
            "today": today,
        }

    async def relational_stats(self) -> dict[str, Any]:
        """Return counts for relational memory."""
        cursor = await self._db.execute("SELECT COUNT(*) FROM relational_entries")
        row = await cursor.fetchone()
        total = row[0] if row else 0

        cursor2 = await self._db.execute(
            "SELECT COUNT(DISTINCT subject) FROM relational_entries"
        )
        row2 = await cursor2.fetchone()
        subjects = row2[0] if row2 else 0

        cursor3 = await self._db.execute(
            "SELECT COUNT(*) FROM relational_entries "
            "WHERE updated_at > datetime('now', '-1 day')"
        )
        row3 = await cursor3.fetchone()
        today = row3[0] if row3 else 0

        return {"total": total, "subjects": subjects, "today": today}

    async def session_stats(self) -> dict[str, Any]:
        """Return session counts and last-active info."""
        cursor = await self._db.execute("SELECT COUNT(*) FROM sessions")
        row = await cursor.fetchone()
        total = row[0] if row else 0

        cursor2 = await self._db.execute(
            "SELECT last_active FROM sessions ORDER BY last_active DESC LIMIT 1"
        )
        row2 = await cursor2.fetchone()
        last_active = row2[0] if row2 else None

        return {"total": total, "last_active": last_active}

    async def skill_stats(self) -> dict[str, Any]:
        """Return skill counts and health breakdown."""
        cursor = await self._db.execute(
            "SELECT COUNT(*), "
            "  SUM(CASE WHEN usage_count > 0 THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN success_rate < 0.6 AND usage_count > 0 THEN 1 ELSE 0 END) "
            "FROM skill_genomes"
        )
        row = await cursor.fetchone()
        total, active, failing = (row or (0, 0, 0))
        return {"total": total, "active": active, "failing": failing}

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _from_search_result(r: MemorySearchResult) -> PalaceResult:
        return PalaceResult(
            scope=r.type,
            key=r.key,
            value=r.value[:200],
            meta=f"conf={r.metadata.get('effective_confidence', r.metadata.get('confidence', 0)):.2f} · {r.updated_at[:10]}",
            score=r.score,
            extra=r.metadata,
        )
