"""
MemoryFacade — unified entry point for the four memory subsystems.

Issue #147 階段 A.  Establishes a single object that owns the four memory
subsystems (``SemanticMemory`` / ``ProceduralMemory`` / ``RelationalMemory``
/ ``EpisodicMemory``) plus the ``MemorySearch`` index.  ``LoomSession``
holds it as ``self._memory`` and forwards subsystem references through
the facade's handles.

Phase A scope (read-only, additive):

* High-level read API: :meth:`search`, :meth:`get_fact`,
  :meth:`query_relations` — covers what the agent ``recall`` / ``memorize``
  / ``query_relations`` tools actually use.
* Subsystem handles (``.semantic`` / ``.procedural`` / ``.relational`` /
  ``.episodic`` / ``.search_index``) for callers that haven't been
  migrated yet.  Existing tools and plugins continue to receive the raw
  subsystems — no caller is forced to change in Phase A.

Phase B (write-path centralisation: ``memorize`` / ``relate``,
embedding-failure handling) and Phase C (caller migration + removal of
the direct subsystem imports) are tracked separately on Issue #147.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from loom.core.memory.episodic import EpisodicMemory
    from loom.core.memory.procedural import ProceduralMemory
    from loom.core.memory.relational import RelationalEntry, RelationalMemory
    from loom.core.memory.search import MemorySearch, MemorySearchResult
    from loom.core.memory.semantic import SemanticEntry, SemanticMemory


class MemoryFacade:
    """Single owner of all memory subsystems with a small read API.

    The facade is instantiated once per ``LoomSession`` after the four
    memory subsystems and the ``MemorySearch`` index have been created.
    It does not reach into any subsystem's private state — each method
    delegates to the public API of the appropriate subsystem.
    """

    def __init__(
        self,
        *,
        semantic: "SemanticMemory",
        procedural: "ProceduralMemory",
        relational: "RelationalMemory",
        episodic: "EpisodicMemory",
        search: "MemorySearch",
    ) -> None:
        self.semantic = semantic
        self.procedural = procedural
        self.relational = relational
        self.episodic = episodic
        self.search_index = search

    # ── read API ─────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        type: Literal["semantic", "skill", "all"] = "all",
        limit: int = 5,
    ) -> list["MemorySearchResult"]:
        """BM25 + embedding ranked retrieval across semantic + procedural memory.

        Wraps :meth:`MemorySearch.recall`.  Equivalent to the agent
        ``recall`` tool, but exposed as a method on the facade so callers
        do not need to know about the search index as a separate object.
        """
        return await self.search_index.recall(query, type=type, limit=limit)

    async def get_fact(self, key: str) -> "SemanticEntry | None":
        """Direct semantic-memory lookup by exact key."""
        return await self.semantic.get(key)

    async def query_relations(
        self,
        subject: str | None = None,
        predicate: str | None = None,
    ) -> list["RelationalEntry"]:
        """Query relational triples by subject and/or predicate.

        Pass ``subject`` to get all predicates for a subject; pass
        ``predicate`` to find all subjects with that predicate; pass both
        for an exact lookup; pass neither to return all entries.  Mirrors
        the underlying :meth:`RelationalMemory.query` signature.
        """
        return await self.relational.query(subject=subject, predicate=predicate)
