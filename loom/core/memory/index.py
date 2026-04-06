"""
MemoryIndex — lightweight directory of what is stored in memory.

Shown at the start of every session so the agent knows what is available
without loading the full memory into context. The agent then uses the
``recall`` tool to pull specific entries on demand.

Skill catalog follows the Agent Skills spec (agentskills.io) progressive
disclosure model:
  Tier 1 (startup) — name + description in ``<available_skills>`` XML
  Tier 2 (on demand) — ``load_skill(name)`` returns full SKILL.md body
  Tier 3 (as needed) — agent reads bundled scripts/references/assets

Rendered format (example)
-------------------------
    Memory Index
    ─────────────────────────────────────────────
    Semantic  : 47 facts   [topics: python, loom, testing, config]
    Episodes  : 8 sessions compressed
    ─────────────────────────────────────────────
    Use recall(query) to retrieve relevant entries.

    <available_skills>
    <skill><name>loom-engineer</name>
    <description>Full implementation cycle from issue to PR.</description></skill>
    </available_skills>
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

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
class SkillCatalogEntry:
    """Tier-1 metadata for a single skill (Agent Skills spec)."""
    name: str
    description: str
    location: str = ""  # absolute path to SKILL.md


@dataclass
class MemoryIndex:
    """Lightweight summary of the current memory state."""
    semantic_count: int = 0
    semantic_topics: list[str] = field(default_factory=list)
    skill_count: int = 0
    skill_tags: list[str] = field(default_factory=list)
    # Issue #56: full skill catalog for progressive disclosure
    skill_catalog: list[SkillCatalogEntry] = field(default_factory=list)
    episode_sessions: int = 0
    relational_count: int = 0
    relational_predicates: list[str] = field(default_factory=list)
    anti_pattern_count: int = 0
    # Issue #26: full loom-self triples for the Self-Portrait section
    self_triples: list[Any] = field(default_factory=list)

    _WIDTH = 45

    def render(self) -> str:
        """Return a compact, fixed-width text block for the system prompt.

        Includes an ``<available_skills>`` XML catalog following the Agent
        Skills spec progressive disclosure model (Tier 1: metadata only).
        """
        topics = ", ".join(self.semantic_topics) if self.semantic_topics else "(none)"
        bar = "─" * self._WIDTH
        lines = [
            "Memory Index",
            bar,
            f"Semantic  : {self.semantic_count} {'fact' if self.semantic_count == 1 else 'facts'}"
            f"   [topics: {topics}]",
            f"Skills    : {self.skill_count} active",
            f"Episodes  : {self.episode_sessions} sessions compressed",
        ]
        if self.relational_count > 0:
            preds = ", ".join(self.relational_predicates) if self.relational_predicates else "(none)"
            lines.append(
                f"Relations : {self.relational_count} {'triple' if self.relational_count == 1 else 'triples'}"
                f"  [predicates: {preds}]"
            )
        if self.anti_pattern_count > 0:
            lines.append(
                f"Anti-patterns: {self.anti_pattern_count} recorded"
                "  [recall 'anti_pattern' to review]"
            )
        lines += [
            bar,
            "Use recall(query) to retrieve relevant entries.",
            "Use memorize(key, value) to store a new fact.",
        ]
        if self.relational_count > 0:
            lines.append("Use query_relations(subject) to look up relationships.")

        # Issue #26: Self-Portrait — show agent's own behavioural notes inline
        if self.self_triples:
            lines.append("")
            lines.append("Self-Portrait (loom-self observations):")
            for t in self.self_triples[:8]:  # cap at 8 to avoid context bloat
                lines.append(f"  [{t.predicate}] {t.object}")

        # Issue #56: Skill catalog — Agent Skills spec progressive disclosure
        # Tier 1: name + description in XML, injected at session start.
        if self.skill_catalog:
            lines.append("")
            lines.append("<available_skills>")
            for entry in self.skill_catalog:
                lines.append("<skill>")
                lines.append(f"  <name>{entry.name}</name>")
                lines.append(f"  <description>{entry.description}</description>")
                lines.append("</skill>")
            lines.append("</available_skills>")
            lines.append("")
            lines.append(
                "The skills listed above provide specialized instructions for "
                "specific tasks. When a task matches a skill's description, call "
                "load_skill(name) to load its full instructions before proceeding."
            )
        elif self.skill_count > 0:
            # Fallback: skills in DB but no catalog entries (no SKILL.md files found)
            tags = ", ".join(self.skill_tags) if self.skill_tags else "(none)"
            lines.append(f"  [tags: {tags}]")

        return "\n".join(lines)

    @property
    def is_empty(self) -> bool:
        return (
            self.semantic_count == 0
            and self.skill_count == 0
            and self.relational_count == 0
            and self.anti_pattern_count == 0
        )


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
        skill_catalog: list[SkillCatalogEntry] | None = None,
    ) -> None:
        self._semantic = semantic
        self._procedural = procedural
        self._episodic = episodic
        self._relational = relational
        self._skill_catalog: list[SkillCatalogEntry] = skill_catalog or []

    async def build(self) -> MemoryIndex:
        """Fetch counts and metadata; return a MemoryIndex."""
        # Semantic facts — true count from DB, sample 500 recent for topic analysis
        semantic_count = await self._semantic.count()
        facts = await self._semantic.list_recent(500)
        semantic_topics = _extract_topics(facts)

        # Distinct compressed sessions — count directly from DB, not from the 500-row sample
        episode_sessions = await self._semantic.count_compressed_sessions()

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

        # Anti-pattern count + Self-Portrait triples (Issue #26)
        anti_pattern_count = 0
        self_triples: list = []
        if self._relational is not None:
            self_entries = await self._relational.query(subject="loom-self")
            anti_pattern_count = sum(
                1 for t in self_entries if t.predicate.startswith("should_avoid")
            )
            # Sort: should_avoid first, then tends_to, then others
            _order = {"should_avoid": 0, "tends_to": 1}
            self_triples = sorted(
                self_entries,
                key=lambda t: (_order.get(t.predicate, 2), t.updated_at),
                reverse=False,
            )

        return MemoryIndex(
            semantic_count=semantic_count,
            semantic_topics=semantic_topics,
            skill_count=skill_count,
            skill_tags=skill_tags,
            skill_catalog=self._skill_catalog,
            episode_sessions=episode_sessions,
            relational_count=relational_count,
            relational_predicates=relational_predicates,
            anti_pattern_count=anti_pattern_count,
            self_triples=self_triples,
        )
