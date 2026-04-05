"""
Memory Search — BM25-based retrieval across all memory types.

Phase 4B: replaces the existing substring-only SemanticMemory.search()
with a proper ranked retrieval system. No external dependencies.

BM25 Formula
------------
    score(D, Q) = Σ IDF(q) × f(q,D)(k1+1) / (f(q,D) + k1(1-b + b·|D|/avgdl))

where IDF(q) = log((N - n(q) + 0.5) / (n(q) + 0.5) + 1)

Usage
-----
    search = MemorySearch(semantic, procedural)
    results = await search.recall("loom configuration", type="semantic", limit=5)
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import datetime
from dataclasses import dataclass, field
from typing import Literal

from loom.core.memory.embeddings import cosine_similarity
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.semantic import SemanticMemory


def _sanitize_fts(query: str) -> str:
    """Sanitize natural language into safe SQLite FTS5 MATCH format (AND logic)."""
    # Remove quotes to avoid syntax issues, then wrap each word in double quotes
    return " ".join(f'"{t}"' for t in query.replace('"', " ").split() if t)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

MemoryType = Literal["semantic", "skill", "all"]


@dataclass
class MemorySearchResult:
    """A single ranked result from memory search."""
    type: str               # "semantic" | "skill"
    key: str                # fact key or skill name
    value: str              # fact value or skill body
    score: float            # BM25 relevance score
    metadata: dict = field(default_factory=dict)
    updated_at: str = ""    # ISO timestamp of last update (empty if unknown)

    def format(self, max_value_len: int = 300) -> str:
        """Human-readable one-entry summary with timestamp and effective confidence."""
        truncated = self.value[:max_value_len]
        if len(self.value) > max_value_len:
            truncated += "…"
        date_hint = f" [{self.updated_at[:10]}]" if self.updated_at else ""
        conf = self.metadata.get("effective_confidence") or self.metadata.get("confidence")
        conf_hint = f" conf={conf:.2f}" if conf is not None else ""
        return f"[{self.type}]{date_hint}{conf_hint} {self.key}\n  {truncated}"


# ---------------------------------------------------------------------------
# MemorySearch
# ---------------------------------------------------------------------------

class MemorySearch:
    """
    Ranked retrieval across semantic facts and procedural skills using BM25.

    BM25 indexes are cached in memory and revalidated on each call via a
    lightweight fingerprint query ``(COUNT, MAX(updated_at))``.  The full
    corpus fetch only runs when the fingerprint changes (new/updated entries).
    """

    def __init__(
        self,
        semantic: SemanticMemory,
        procedural: ProceduralMemory,
    ) -> None:
        self._semantic = semantic
        self._procedural = procedural

    async def recall(
        self,
        query: str,
        type: MemoryType = "all",
        limit: int = 5,
    ) -> list[MemorySearchResult]:
        """
        Return the top-*limit* memory entries most relevant to *query*.

        BM25 is lexical (keyword-based).  When no terms overlap between the
        query and stored facts — e.g. cross-language queries or very generic
        phrases — the ranked search returns no results.  In that case we fall
        back to recency ordering so the caller always gets something useful.

        Parameters
        ----------
        query:  Natural-language search query.
        type:   Which memory store(s) to search: "semantic", "skill", or "all".
        limit:  Maximum number of results to return (across all types).
        """
        if not query.strip():
            return []

        # ── Tier 1: embedding cosine similarity (language-agnostic) ──────────
        # Only runs when the SemanticMemory instance has an EmbeddingProvider
        # configured.  Any exception (network, API error) falls through to BM25.
        if self._semantic.has_embeddings and type in ("semantic", "all"):
            try:
                emb_results = await self._search_semantic_embedding(query, limit)
                if emb_results:
                    # Skills are not embedding-indexed; mix in BM25 skill results
                    if type == "all":
                        skill_results = await self._search_skills(query, limit)
                        combined = emb_results + skill_results
                        combined.sort(key=lambda r: r.score, reverse=True)
                        return combined[:limit]
                    return emb_results
            except Exception:
                pass  # Fall through to BM25

        # ── Tier 2: BM25 keyword search ───────────────────────────────────────
        results: list[MemorySearchResult] = []
        if type in ("semantic", "all"):
            results.extend(await self._search_semantic(query, limit))
        if type in ("skill", "all"):
            results.extend(await self._search_skills(query, limit))

        results.sort(key=lambda r: r.score, reverse=True)
        ranked = results[:limit]

        # ── Tier 3: recency fallback ──────────────────────────────────────────
        if not ranked:
            ranked = await self._recent_fallback(type, limit)

        return ranked

    async def _search_semantic_embedding(
        self, query: str, limit: int
    ) -> list[MemorySearchResult]:
        """Rank semantic entries by cosine similarity using sqlite-vec."""
        provider = self._semantic._embeddings
        if provider is None:
            return []

        query_vectors = await provider.embed([query])
        if not query_vectors:
            return []
        query_vec = query_vectors[0]

        cursor = await self._semantic._db.execute(
            """
            SELECT id, key, value, confidence, source, metadata, created_at, updated_at,
                   1.0 - vec_distance_cosine(embedding, ?) AS score
            FROM semantic_entries
            WHERE embedding IS NOT NULL
            ORDER BY vec_distance_cosine(embedding, ?) ASC
            LIMIT ?
            """,
            (json.dumps(query_vec), json.dumps(query_vec), limit)
        )
        rows = await cursor.fetchall()
        
        from loom.core.memory.semantic import SemanticEntry

        results: list[MemorySearchResult] = []
        for r in rows:
            entry = SemanticEntry(
                id=r[0], key=r[1], value=r[2], confidence=r[3],
                source=r[4], metadata=json.loads(r[5]),
                created_at=datetime.fromisoformat(r[6]),
                updated_at=datetime.fromisoformat(r[7]),
            )
            score = r[8]
            if score > 0.0:
                results.append(
                    MemorySearchResult(
                        type="semantic",
                        key=entry.key,
                        value=entry.value,
                        score=score,
                        metadata={
                            "confidence": entry.confidence,
                            "effective_confidence": entry.effective_confidence(),
                            "method": "embedding",
                        },
                        updated_at=entry.updated_at.isoformat() if entry.updated_at else "",
                    )
                )
        return results

    async def _recent_fallback(
        self, type: MemoryType, limit: int
    ) -> list[MemorySearchResult]:
        """Return the most recently updated entries with score=0 (recency order)."""
        results: list[MemorySearchResult] = []

        if type in ("semantic", "all"):
            entries = await self._semantic.list_recent(limit)
            results.extend(
                MemorySearchResult(
                    type="semantic",
                    key=e.key,
                    value=e.value,
                    score=0.0,
                    metadata={
                        "confidence": e.confidence,
                        "effective_confidence": e.effective_confidence(),
                        "fallback": True,
                    },
                    updated_at=e.updated_at.isoformat() if e.updated_at else "",
                )
                for e in entries
            )

        if type in ("skill", "all"):
            skills = await self._procedural.list_active()
            results.extend(
                MemorySearchResult(
                    type="skill",
                    key=s.name,
                    value=s.body,
                    score=0.0,
                    metadata={"confidence": s.confidence, "tags": s.tags, "fallback": True},
                    updated_at=s.updated_at.isoformat() if s.updated_at else "",
                )
                for s in skills[:limit]
            )

        return results[:limit]

    # ------------------------------------------------------------------

    async def _search_semantic(self, query: str, limit: int) -> list[MemorySearchResult]:
        if not query.strip():
            return []
            
        safe_query = _sanitize_fts(query)
        if not safe_query:
            return []

        # FTS5 returns negative scores for bm25 by default, smaller = better.
        # We order by rank and return absolute values for positive compatibility.
        cursor = await self._semantic._db.execute(
            """
            SELECT
                e.id, e.key, e.value, e.confidence, e.source, e.metadata, e.created_at, e.updated_at,
                bm25(semantic_fts) AS fts_score
            FROM semantic_fts
            JOIN semantic_entries e ON semantic_fts.rowid = e.rowid
            WHERE semantic_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (safe_query, limit)
        )
        rows = await cursor.fetchall()

        from loom.core.memory.semantic import SemanticEntry
        results: list[MemorySearchResult] = []
        for r in rows:
            entry = SemanticEntry(
                id=r[0], key=r[1], value=r[2], confidence=r[3],
                source=r[4], metadata=json.loads(r[5]),
                created_at=datetime.fromisoformat(r[6]),
                updated_at=datetime.fromisoformat(r[7]),
            )
            # Convert negative rank to positive score
            score = abs(r[8]) if r[8] else 0.0
            results.append(
                MemorySearchResult(
                    type="semantic",
                    key=entry.key,
                    value=entry.value,
                    score=score,
                    metadata={
                        "confidence": entry.confidence,
                        "effective_confidence": entry.effective_confidence(),
                    },
                    updated_at=entry.updated_at.isoformat() if entry.updated_at else "",
                )
            )
        return results

    async def _search_skills(self, query: str, limit: int) -> list[MemorySearchResult]:
        if not query.strip():
            return []
            
        safe_query = _sanitize_fts(query)
        if not safe_query:
            return []

        cursor = await self._procedural._db.execute(
            """
            SELECT
                g.id, g.name, g.version, g.confidence, g.usage_count, 
                g.success_rate, g.parent_skill, g.deprecation_threshold, 
                g.tags, g.body, g.created_at, g.updated_at,
                bm25(skill_fts) AS fts_score
            FROM skill_fts
            JOIN skill_genomes g ON skill_fts.rowid = g.rowid
            WHERE skill_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (safe_query, limit)
        )
        rows = await cursor.fetchall()

        from loom.core.memory.procedural import SkillGenome
        results: list[MemorySearchResult] = []
        for r in rows:
            skill = SkillGenome(
                id=r[0], name=r[1], version=r[2], confidence=r[3], usage_count=r[4],
                success_rate=r[5], parent_skill=r[6], deprecation_threshold=r[7],
                tags=json.loads(r[8]), body=r[9],
                created_at=datetime.fromisoformat(r[10]),
                updated_at=datetime.fromisoformat(r[11]),
            )
            if skill.confidence <= skill.deprecation_threshold:
                continue

            score = abs(r[12]) if r[12] else 0.0
            results.append(
                MemorySearchResult(
                    type="skill",
                    key=skill.name,
                    value=skill.body,
                    score=score,
                    metadata={"confidence": skill.confidence, "tags": skill.tags},
                    updated_at=skill.updated_at.isoformat() if skill.updated_at else "",
                )
            )

        return results[:limit]
