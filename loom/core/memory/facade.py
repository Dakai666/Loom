"""
MemoryFacade — unified entry point for the four memory subsystems.

Issue #147 — single object that owns the four memory subsystems
(``SemanticMemory`` / ``ProceduralMemory`` / ``RelationalMemory`` /
``EpisodicMemory``) plus the ``MemorySearch`` index and an optional
``MemoryGovernor``.  ``LoomSession`` holds it as ``self._memory`` and
forwards subsystem references through the facade's handles.

Phase A (read API): :meth:`search` / :meth:`get_fact` /
:meth:`query_relations` — what the agent ``recall`` / ``query_relations``
tools need.

Phase B (write API + agent tool migration):

* :meth:`memorize` — semantic write through ``MemoryGovernor`` (or a
  direct ``SemanticMemory.upsert`` fallback when no governor is wired).
  Surfaces embedding-write failures via a structured WARN log so callers
  no longer need to inspect ``MemoryHealthTracker`` themselves.
* :meth:`relate` — relational triple upsert.
* :meth:`prune_decayed` — wraps ``SemanticMemory.prune_decayed`` so the
  ``memory_prune`` cron tool (and any future caller) goes through one
  entry point.

Phase C (caller migration + removal of the direct subsystem imports) is
tracked separately on Issue #147.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from loom.core.memory.episodic import EpisodicMemory
    from loom.core.memory.governance import GovernedWriteResult, MemoryGovernor
    from loom.core.memory.procedural import ProceduralMemory
    from loom.core.memory.relational import RelationalEntry, RelationalMemory
    from loom.core.memory.search import MemorySearch, MemorySearchResult
    from loom.core.memory.semantic import SemanticEntry, SemanticMemory


logger = logging.getLogger(__name__)


class MemoryFacade:
    """Single owner of all memory subsystems with a small read+write API.

    The facade is instantiated once per ``LoomSession`` after the four
    memory subsystems, the ``MemorySearch`` index, and (optionally) the
    ``MemoryGovernor`` have been created.  It does not reach into any
    subsystem's private state — each method delegates to the public API
    of the appropriate subsystem.
    """

    def __init__(
        self,
        *,
        semantic: "SemanticMemory",
        procedural: "ProceduralMemory",
        relational: "RelationalMemory",
        episodic: "EpisodicMemory",
        search: "MemorySearch",
        governor: "MemoryGovernor | None" = None,
    ) -> None:
        self.semantic = semantic
        self.procedural = procedural
        self.relational = relational
        self.episodic = episodic
        self.search_index = search
        self.governor = governor

    # ── read API ─────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        kind: Literal["semantic", "skill", "all"] = "all",
        limit: int = 5,
    ) -> list["MemorySearchResult"]:
        """BM25 + embedding ranked retrieval across semantic + procedural memory.

        Wraps :meth:`MemorySearch.recall`.  Equivalent to the agent
        ``recall`` tool, but exposed as a method on the facade so callers
        do not need to know about the search index as a separate object.

        ``kind`` (formerly ``type`` in Phase A) selects which memory
        backend to hit: ``"semantic"`` facts only, ``"skill"`` for
        procedural skills only, or ``"all"`` (default).  Renamed to
        avoid shadowing the ``type`` builtin.
        """
        return await self.search_index.recall(query, type=kind, limit=limit)

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

    # ── write API ────────────────────────────────────────────────────────

    async def memorize(self, entry: "SemanticEntry") -> "GovernedWriteResult":
        """Persist a semantic fact through the governance pipeline.

        When a :class:`MemoryGovernor` is wired, delegates to
        :meth:`MemoryGovernor.governed_upsert` (trust classification +
        contradiction detection + audit log).  Without a governor — only
        in tests / minimal setups — falls back to a plain
        :meth:`SemanticMemory.upsert` and synthesises an equivalent
        ``GovernedWriteResult`` so the caller's contract stays uniform.

        Embedding failures are silent inside ``SemanticMemory.upsert`` by
        design (Issue #133 / #147 contract), but the
        ``MemoryHealthTracker`` records them.  This method snapshots the
        tracker's ``embedding_write`` failure count before and after the
        write and emits a structured WARN log if a new failure landed.
        Callers no longer need to poke at the health tracker directly.
        """
        from loom.core.memory.governance import GovernedWriteResult

        before = self._embedding_failure_count()

        if self.governor is not None:
            result = await self.governor.governed_upsert(entry)
        else:
            conflicted = await self.semantic.upsert(entry)
            result = GovernedWriteResult(
                written=True,
                trust_tier="unknown",
                adjusted_confidence=entry.confidence,
                contradictions_found=0,
                resolution="replaced" if conflicted else None,
            )

        after = self._embedding_failure_count()
        if after > before:
            logger.warning(
                "memorize: embedding write failed for key=%r — entry "
                "stored but semantic search will miss it (see "
                "memory_health for details)",
                entry.key,
            )

        return result

    async def relate(self, entry: "RelationalEntry") -> None:
        """Upsert a relational (subject, predicate, object) triple.

        Thin wrapper around :meth:`RelationalMemory.upsert` so callers
        that already hold a facade do not need a second handle for
        relational writes.  Relational memory has no governance hooks
        today; this method exists so future governance can be added in
        one place.
        """
        await self.relational.upsert(entry)

    async def prune_decayed(
        self,
        threshold: float = 0.1,
        dry_run: bool = False,
    ) -> dict:
        """Prune semantic entries whose effective confidence has decayed.

        Wraps :meth:`SemanticMemory.prune_decayed`.  Returns the same
        ``{examined, pruned, retained, threshold, dry_run}`` dict.
        """
        return await self.semantic.prune_decayed(
            threshold=threshold, dry_run=dry_run,
        )

    # ── internal helpers ────────────────────────────────────────────────

    def _embedding_failure_count(self) -> int:
        """Read the current ``embedding_write`` failure count from the
        governor's health tracker, or 0 when no governor is wired.

        Used by :meth:`memorize` to detect whether a new failure landed
        during the write so it can be surfaced through a structured log.
        """
        if self.governor is None:
            return 0
        op = self.governor.health.report().operations.get("embedding_write")
        return op.failure_count if op else 0
