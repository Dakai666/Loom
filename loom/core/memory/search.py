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

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from loom.core.memory.embeddings import cosine_similarity
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.semantic import SemanticMemory


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """Lowercase and split on non-word characters."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return [t for t in text.split() if t]


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------

class BM25:
    """
    Okapi BM25 ranking function over an in-memory corpus.

    Parameters
    ----------
    k1:  Term frequency saturation factor (default 1.5).
    b:   Length normalization factor (default 0.75).
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._docs: list[list[str]] = []
        self._idf: dict[str, float] = {}
        self._avgdl: float = 0.0

    def index(self, documents: list[str]) -> None:
        """Build the index from a list of document strings."""
        self._docs = [_tokenize(doc) for doc in documents]
        n = len(self._docs)
        if n == 0:
            self._avgdl = 0.0
            self._idf = {}
            return

        self._avgdl = sum(len(d) for d in self._docs) / n

        # Document frequency per term
        df: dict[str, int] = {}
        for tokens in self._docs:
            for term in set(tokens):
                df[term] = df.get(term, 0) + 1

        # Smoothed IDF (always positive)
        self._idf = {
            term: math.log((n - count + 0.5) / (count + 0.5) + 1)
            for term, count in df.items()
        }

    def score(self, query: str, doc_idx: int) -> float:
        """Return the BM25 score for one document."""
        if doc_idx >= len(self._docs) or self._avgdl == 0:
            return 0.0

        tokens = self._docs[doc_idx]
        dl = len(tokens)
        if dl == 0:
            return 0.0

        tf = Counter(tokens)
        total = 0.0

        for term in _tokenize(query):
            idf = self._idf.get(term, 0.0)
            if idf == 0.0:
                continue
            f = tf.get(term, 0)
            num = f * (self.k1 + 1)
            den = f + self.k1 * (1.0 - self.b + self.b * dl / self._avgdl)
            total += idf * num / den

        return total

    def top_k(self, query: str, k: int = 5) -> list[tuple[int, float]]:
        """
        Return (doc_idx, score) pairs for the top-k highest-scoring documents.
        Only documents with score > 0 are included.
        """
        scores = [
            (i, self.score(query, i))
            for i in range(len(self._docs))
        ]
        scores = [(i, s) for i, s in scores if s > 0.0]
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:k]


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

    def format(self, max_value_len: int = 300) -> str:
        """Human-readable one-entry summary."""
        truncated = self.value[:max_value_len]
        if len(self.value) > max_value_len:
            truncated += "…"
        return f"[{self.type}] {self.key}\n  {truncated}"


# ---------------------------------------------------------------------------
# MemorySearch
# ---------------------------------------------------------------------------

class MemorySearch:
    """
    Ranked retrieval across semantic facts and procedural skills using BM25.

    Each call to ``recall()`` fetches the full corpus from SQLite and builds
    a fresh BM25 index. Suitable for corpora up to ~10 000 entries.
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
        """Rank semantic entries by cosine similarity to the query embedding."""
        provider = self._semantic._embeddings
        if provider is None:
            return []

        query_vectors = await provider.embed([query])
        if not query_vectors:
            return []
        query_vec = query_vectors[0]

        entries_with_vecs = await self._semantic.list_with_embeddings(500)

        scored: list[tuple[float, int]] = []
        for i, (_, vec) in enumerate(entries_with_vecs):
            if vec is None:
                continue
            score = cosine_similarity(query_vec, vec)
            if score > 0.0:
                scored.append((score, i))

        scored.sort(reverse=True)

        return [
            MemorySearchResult(
                type="semantic",
                key=entries_with_vecs[i][0].key,
                value=entries_with_vecs[i][0].value,
                score=score,
                metadata={
                    "confidence": entries_with_vecs[i][0].confidence,
                    "method": "embedding",
                },
            )
            for score, i in scored[:limit]
        ]

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
                    metadata={"confidence": e.confidence, "fallback": True},
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
                )
                for s in skills[:limit]
            )

        return results[:limit]

    # ------------------------------------------------------------------

    async def _search_semantic(self, query: str, limit: int) -> list[MemorySearchResult]:
        entries = await self._semantic.list_recent(500)
        if not entries:
            return []

        # Index key + value so the query can match on either
        docs = [f"{e.key} {e.value}" for e in entries]
        bm25 = BM25()
        bm25.index(docs)

        return [
            MemorySearchResult(
                type="semantic",
                key=entries[i].key,
                value=entries[i].value,
                score=score,
                metadata={"confidence": entries[i].confidence},
            )
            for i, score in bm25.top_k(query, k=limit)
        ]

    async def _search_skills(self, query: str, limit: int) -> list[MemorySearchResult]:
        skills = await self._procedural.list_active()
        if not skills:
            return []

        # Index name + tags + body
        docs = [
            f"{s.name} {' '.join(s.tags)} {s.body}"
            for s in skills
        ]
        bm25 = BM25()
        bm25.index(docs)

        return [
            MemorySearchResult(
                type="skill",
                key=skills[i].name,
                value=skills[i].body,
                score=score,
                metadata={"confidence": skills[i].confidence, "tags": skills[i].tags},
            )
            for i, score in bm25.top_k(query, k=limit)
        ]
