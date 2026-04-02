"""
MemoryIndex — lightweight directory of what is stored in memory.

Shown at the start of every session so the agent knows what is available
without loading the full memory into context. The agent then uses the
``recall`` tool to pull specific entries on demand.

Rendered format (example)
-------------------------
    Memory Index
    ─────────────────────────────────────────────
    Semantic  : 47 facts   [topics: python, loom, testing, config]
    Skills    : 12 active  [tags: refactor, bash, git, python]
    Episodes  : 8 sessions compressed
    ─────────────────────────────────────────────
    Use recall(query) to retrieve relevant entries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from loom.core.memory.episodic import EpisodicMemory
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.relational import RelationalMemory
from loom.core.memory.semantic import SemanticEntry, SemanticMemory


# ---------------------------------------------------------------------------
# Common English stopwords for topic extraction
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "has", "have", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "can", "shall", "to", "of", "in",
    "on", "at", "for", "with", "by", "from", "and", "or", "not", "no",
    "it", "its", "this", "that", "these", "those", "as", "so", "if",
    "when", "then", "than", "there", "here", "their", "they", "we",
    "you", "your", "our", "my", "his", "her", "new", "use", "used",
    "also", "each", "which", "what", "how", "any", "all", "more",
})


def _extract_topics(facts: list[SemanticEntry], max_topics: int = 6) -> list[str]:
    """Return the most frequent content words across semantic fact values."""
    freq: dict[str, int] = {}
    for fact in facts:
        words = re.sub(r"[^\w\s]", " ", fact.value.lower()).split()
        for w in words:
            if len(w) > 3 and w not in _STOPWORDS:
                freq[w] = freq.get(w, 0) + 1

    ranked = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    return [w for w, _ in ranked[:max_topics]]


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class MemoryIndex:
    """Lightweight summary of the current memory state."""
    semantic_count: int = 0
    semantic_topics: list[str] = field(default_factory=list)
    skill_count: int = 0
    skill_tags: list[str] = field(default_factory=list)
    episode_sessions: int = 0
    relational_count: int = 0
    relational_predicates: list[str] = field(default_factory=list)

    _WIDTH = 45

    def render(self) -> str:
        """Return a compact, fixed-width text block for the system prompt."""
        topics = ", ".join(self.semantic_topics) if self.semantic_topics else "(none)"
        tags = ", ".join(self.skill_tags) if self.skill_tags else "(none)"
        bar = "─" * self._WIDTH
        lines = [
            "Memory Index",
            bar,
            f"Semantic  : {self.semantic_count} {'fact' if self.semantic_count == 1 else 'facts'}"
            f"   [topics: {topics}]",
            f"Skills    : {self.skill_count} active  [tags: {tags}]",
            f"Episodes  : {self.episode_sessions} sessions compressed",
        ]
        if self.relational_count > 0:
            preds = ", ".join(self.relational_predicates) if self.relational_predicates else "(none)"
            lines.append(
                f"Relations : {self.relational_count} {'triple' if self.relational_count == 1 else 'triples'}"
                f"  [predicates: {preds}]"
            )
        lines += [
            bar,
            "Use recall(query) to retrieve relevant entries.",
            "Use memorize(key, value) to store a new fact.",
        ]
        if self.relational_count > 0:
            lines.append("Use query_relations(subject) to look up relationships.")
        return "\n".join(lines)

    @property
    def is_empty(self) -> bool:
        return self.semantic_count == 0 and self.skill_count == 0 and self.relational_count == 0


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class MemoryIndexer:
    """
    Queries the memory stores and builds a MemoryIndex snapshot.

    Called once at the start of each session.
    """

    def __init__(
        self,
        semantic: SemanticMemory,
        procedural: ProceduralMemory,
        episodic: EpisodicMemory | None = None,
        relational: RelationalMemory | None = None,
    ) -> None:
        self._semantic = semantic
        self._procedural = procedural
        self._episodic = episodic
        self._relational = relational

    async def build(self) -> MemoryIndex:
        """Fetch counts and metadata; return a MemoryIndex."""
        # Semantic facts (fetch up to 500 recent for topic analysis)
        facts = await self._semantic.list_recent(500)
        semantic_count = len(facts)
        semantic_topics = _extract_topics(facts)

        # Distinct compressed sessions — normalize "session:<id>:fact:<n>" → "session:<id>"
        episode_sessions = len({
            ":".join(f.source.split(":")[:2])
            for f in facts
            if f.source and f.source.startswith("session:")
        })

        # Active skills
        skills = await self._procedural.list_active()
        skill_count = len(skills)

        all_tags: set[str] = set()
        for s in skills:
            all_tags.update(s.tags)
        skill_tags = sorted(all_tags)[:10]

        # Relational triples
        relational_count = 0
        relational_predicates: list[str] = []
        if self._relational is not None:
            triples = await self._relational.query()
            relational_count = len(triples)
            seen: dict[str, int] = {}
            for t in triples:
                seen[t.predicate] = seen.get(t.predicate, 0) + 1
            relational_predicates = sorted(seen, key=lambda p: seen[p], reverse=True)[:8]

        return MemoryIndex(
            semantic_count=semantic_count,
            semantic_topics=semantic_topics,
            skill_count=skill_count,
            skill_tags=skill_tags,
            episode_sessions=episode_sessions,
            relational_count=relational_count,
            relational_predicates=relational_predicates,
        )
