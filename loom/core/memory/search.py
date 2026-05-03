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
import logging
import math
import re
from collections import Counter
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

from loom.core.memory.embeddings import cosine_similarity
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.semantic import (
    SemanticMemory,
    _SELECT_COLS as _SEMANTIC_SELECT_COLS,
    _row_to_entry as _semantic_row_to_entry,
)


def _sanitize_fts(query: str) -> str:
    """Sanitize natural language into safe SQLite FTS5 MATCH format (AND logic)."""
    # Remove quotes to avoid syntax issues, then wrap each word in double quotes
    return " ".join(f'"{t}"' for t in query.replace('"', " ").split() if t)


def _axis_filter_sql(
    domain: str | None,
    temporal: str | None,
    prefix: str = "",
) -> tuple[str, tuple[str, ...]]:
    """Build a SQL fragment + params for optional (domain, temporal) filters.

    Returns ``("", ())`` when both axes are None — callers can splice the
    fragment after an existing ``WHERE`` clause without needing to branch
    on whether any filter was supplied. ``prefix`` is prepended to the
    column names for queries that join multiple tables (e.g. ``"e."``).
    """
    parts: list[str] = []
    params: list[str] = []
    if domain:
        parts.append(f"AND {prefix}domain = ?")
        params.append(domain)
    if temporal:
        parts.append(f"AND {prefix}temporal = ?")
        params.append(temporal)
    return (" " + " ".join(parts) if parts else ""), tuple(params)


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
        self._health: Any = None  # Optional MemoryHealthTracker, set post-init

    async def recall(
        self,
        query: str,
        type: MemoryType = "all",
        limit: int = 5,
        domain: str | None = None,
        temporal: str | None = None,
    ) -> list[MemorySearchResult]:
        """
        Return the top-*limit* memory entries most relevant to *query*.

        BM25 is lexical (keyword-based).  When no terms overlap between the
        query and stored facts — e.g. cross-language queries or very generic
        phrases — the ranked search returns no results.  In that case we fall
        back to recency ordering so the caller always gets something useful.

        Parameters
        ----------
        query:    Natural-language search query.
        type:     Which memory store(s) to search: "semantic", "skill", or "all".
        limit:    Maximum number of results to return (across all types).
        domain:   Optional Memory-Ontology axis filter (issue #281). When set,
                  only semantic entries with this domain are returned. Skills
                  are unaffected — domain/temporal apply to facts, not skills.
        temporal: Optional temporal axis filter (same semantics as ``domain``).
        """
        if not query.strip():
            return []

        # ── Tier 1: embedding cosine similarity (language-agnostic) ──────────
        # Only runs when the SemanticMemory instance has an EmbeddingProvider
        # configured.  Any exception (network, API error) falls through to BM25.
        if self._semantic.has_embeddings and type in ("semantic", "all"):
            try:
                emb_results = await self._search_semantic_embedding(
                    query, limit, domain=domain, temporal=temporal,
                )
                if emb_results:
                    # Skills are not embedding-indexed; mix in BM25 skill results
                    if type == "all":
                        skill_results = await self._search_skills(query, limit)
                        combined = emb_results + skill_results
                        combined.sort(key=lambda r: r.score, reverse=True)
                        ranked = combined[:limit]
                    else:
                        ranked = emb_results
                    await self._mark_accessed(ranked)
                    return ranked
            except Exception as exc:
                logger.warning(
                    "Embedding search failed, degrading to BM25: %s", exc,
                )
                if self._health:
                    self._health.record_failure("embedding_search", str(exc))

        # ── Tier 2: BM25 keyword search ───────────────────────────────────────
        results: list[MemorySearchResult] = []
        if type in ("semantic", "all"):
            results.extend(
                await self._search_semantic(
                    query, limit, domain=domain, temporal=temporal,
                )
            )
        if type in ("skill", "all"):
            results.extend(await self._search_skills(query, limit))

        results.sort(key=lambda r: r.score, reverse=True)
        ranked = results[:limit]

        # ── Tier 3: recency fallback ──────────────────────────────────────────
        if not ranked:
            ranked = await self._recent_fallback(type, limit)

        await self._mark_accessed(ranked)
        return ranked

    async def _mark_accessed(self, results: list[MemorySearchResult]) -> None:
        """Bump last_accessed_at for semantic hits (Memory Ontology v0.1)."""
        keys = [r.key for r in results if r.type == "semantic"]
        if keys:
            await self._semantic.mark_accessed(keys)

    async def _search_semantic_embedding(
        self, query: str, limit: int,
        domain: str | None = None,
        temporal: str | None = None,
    ) -> list[MemorySearchResult]:
        """Rank semantic entries by cosine similarity using sqlite-vec."""
        provider = self._semantic._embeddings
        if provider is None:
            return []

        query_vectors = await provider.embed([query])
        if not query_vectors:
            return []
        query_vec = query_vectors[0]

        where, axis_params = _axis_filter_sql(domain, temporal)
        cursor = await self._semantic._db.execute(
            f"""
            SELECT {_SEMANTIC_SELECT_COLS},
                   1.0 - vec_distance_cosine(embedding, ?) AS score
            FROM semantic_entries
            WHERE embedding IS NOT NULL{where}
            ORDER BY vec_distance_cosine(embedding, ?) ASC
            LIMIT ?
            """,
            (json.dumps(query_vec), *axis_params, json.dumps(query_vec), limit)
        )
        rows = await cursor.fetchall()

        results: list[MemorySearchResult] = []
        for r in rows:
            # _SELECT_COLS produces 11 columns; index 11 holds the score.
            entry = _semantic_row_to_entry(r[:11])
            score = r[11]
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

    async def _search_semantic(
        self, query: str, limit: int,
        domain: str | None = None,
        temporal: str | None = None,
    ) -> list[MemorySearchResult]:
        if not query.strip():
            return []

        safe_query = _sanitize_fts(query)
        if not safe_query:
            return []

        where, axis_params = _axis_filter_sql(domain, temporal, prefix="e.")
        # SELECT mirrors _SEMANTIC_SELECT_COLS aliased onto the joined table
        # so _semantic_row_to_entry can parse rows uniformly with the
        # embedding path.
        select_cols = ", ".join(f"e.{c.strip()}" for c in _SEMANTIC_SELECT_COLS.split(","))
        # FTS5 returns negative scores for bm25 by default, smaller = better.
        # We order by rank and return absolute values for positive compatibility.
        cursor = await self._semantic._db.execute(
            f"""
            SELECT
                {select_cols},
                bm25(semantic_fts) AS fts_score
            FROM semantic_fts
            JOIN semantic_entries e ON semantic_fts.rowid = e.rowid
            WHERE semantic_fts MATCH ?{where}
            ORDER BY rank
            LIMIT ?
            """,
            (safe_query, *axis_params, limit)
        )
        rows = await cursor.fetchall()

        results: list[MemorySearchResult] = []
        for r in rows:
            entry = _semantic_row_to_entry(r[:11])
            # Convert negative rank to positive score
            score = abs(r[11]) if r[11] else 0.0
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
