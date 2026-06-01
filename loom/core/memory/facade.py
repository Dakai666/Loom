"""
MemoryFacade — unified entry point for the memory subsystems.

Issue #147 — single object that owns the memory subsystems
(``SemanticMemory`` / ``ProceduralMemory`` / ``EpisodicMemory``) plus
the ``MemorySearch`` index and an optional ``MemoryGovernor``.
``LoomSession`` holds it as ``self._memory`` and forwards subsystem
references through the facade's handles.

Read API: :meth:`search` / :meth:`get_fact` / :meth:`recall_period`.
Agent recall is the *sole* path for surfacing relational triples
(issue #451 phase B — ``query_relations`` retired).

Write API:

* :meth:`memorize` — semantic write through ``MemoryGovernor`` (or a
  direct ``SemanticMemory.upsert`` fallback when no governor is wired).
  Surfaces embedding-write failures via a structured WARN log so callers
  no longer need to inspect ``MemoryHealthTracker`` themselves.
* :meth:`relate` — relational triple upsert. Since #451 phase B this
  routes through :func:`loom.core.memory.relational_bridge.upsert_triple`
  onto the semantic store; ``relational_entries`` no longer exists.
* :meth:`prune_decayed` — wraps ``SemanticMemory.prune_decayed`` so the
  ``memory_prune`` cron tool (and any future caller) goes through one
  entry point.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from loom.core.ledger import LedgerEmitter
    from loom.core.memory.episodic import EpisodicMemory
    from loom.core.memory.governance import GovernedWriteResult, MemoryGovernor
    from loom.core.memory.procedural import ProceduralMemory
    from loom.core.memory.relational_bridge import RelationalEntry
    from loom.core.memory.search import MemorySearch, MemorySearchResult
    from loom.core.memory.session_log import SessionLog
    from loom.core.memory.semantic import SemanticEntry, SemanticMemory


logger = logging.getLogger(__name__)


def _content_digest(value: str | bytes) -> str:
    """Stable sha256 digest used by ledger memory_op events (doc/53 §5.6)."""
    if isinstance(value, str):
        value = value.encode("utf-8")
    return "sha256:" + hashlib.sha256(value).hexdigest()


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
        episodic: "EpisodicMemory",
        search: "MemorySearch",
        session_log: "SessionLog | None" = None,
        governor: "MemoryGovernor | None" = None,
        ledger_emitter: "LedgerEmitter | None" = None,
    ) -> None:
        self.semantic = semantic
        self.procedural = procedural
        self.episodic = episodic
        self.search_index = search
        self.session_log = session_log
        self.governor = governor
        # ledger_emitter is None in the dual-emit transition until
        # LoomSession.start() wires one (Step 2 commit 6). Tests and
        # standalone callers may also leave it None — emits are then no-ops.
        self.ledger_emitter = ledger_emitter

    # ── read API ─────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        kind: Literal["semantic", "skill", "all"] = "all",
        limit: int = 5,
        domain: str | None = None,
        temporal: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list["MemorySearchResult"]:
        """BM25 + embedding ranked retrieval across semantic + procedural memory.

        Wraps :meth:`MemorySearch.recall`.  Equivalent to the agent
        ``recall`` tool, but exposed as a method on the facade so callers
        do not need to know about the search index as a separate object.

        ``kind`` (formerly ``type`` in Phase A) selects which memory
        backend to hit: ``"semantic"`` facts only, ``"skill"`` for
        procedural skills only, or ``"all"`` (default).  Renamed to
        avoid shadowing the ``type`` builtin.

        ``domain`` and ``temporal`` are optional Memory-Ontology filters
        (issue #281) — see :meth:`MemorySearch.recall` for semantics.
        """
        results = await self.search_index.recall(
            query, type=kind, limit=limit,
            domain=domain, temporal=temporal,
            since=since, until=until,
        )
        await self._emit_memory_op(
            operation="read",
            type_summary=f"search:{kind}",
            trigger="agent_search",
            memory_ids=[r.id for r in results if getattr(r, "id", None)] or None,
        )
        return results

    async def get_fact(self, key: str) -> "SemanticEntry | None":
        """Direct semantic-memory lookup by exact key.

        Follows a consolidation redirect (#494): a key that was merged away
        resolves to its survivor so the old-key fallback works as designed.
        """
        entry = await self.semantic.resolve_redirect(key)
        await self._emit_memory_op(
            operation="read",
            memory_id=getattr(entry, "id", None) if entry else None,
            type_summary="semantic_fact",
            trigger="agent_get_fact",
        )
        return entry

    async def recall_period(
        self,
        *,
        since: datetime,
        until: datetime | None = None,
        session_id: str | None = None,
        limit: int = 10,
        include_episodic: bool = True,
        include_sessions: bool = False,
    ) -> dict[str, list]:
        """Return grouped memory evidence for a time window.

        Semantic facts are always included. Episodic events and session
        metadata are optional so callers can start with distilled facts and
        deepen only when needed.
        """
        capped = min(max(int(limit), 1), 20)
        semantic = await self.semantic.list_between(
            since, until, limit=capped,
        )
        episodic = (
            await self.episodic.list_between(
                since, until, session_id=session_id, limit=capped,
            )
            if include_episodic else []
        )
        sessions = (
            await self.session_log.list_sessions_between(
                since, until, limit=capped,
            )
            if include_sessions and self.session_log is not None else []
        )
        messages = (
            await self.session_log.messages_between(
                since, until, session_id=session_id, limit=capped,
            )
            if include_sessions and self.session_log is not None else []
        )
        await self._emit_memory_op(
            operation="read",
            type_summary="period_recall",
            trigger="agent_recall_period",
        )
        return {
            "semantic": semantic,
            "episodic": episodic,
            "sessions": sessions,
            "messages": messages,
        }

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

        await self._emit_memory_op(
            operation="write",
            memory_id=entry.key,  # SemanticEntry uses key as identity
            type_summary="semantic_fact",
            trust_tier=result.trust_tier,
            content_digest=_content_digest(entry.value),
            trigger="agent_memorize",
        )
        return result

    async def relate(self, entry: "RelationalEntry") -> None:
        """Upsert a relational (subject, predicate, object) triple.

        Since #451 phase B this routes through the bridge into the
        semantic store; ``relational_entries`` no longer exists. The
        bridge encodes the triple as a ``SemanticEntry`` keyed
        ``rel:{subject}::{predicate}``, so ``recall`` surfaces it
        naturally with ``type="relational"``.
        """
        from loom.core.memory.relational_bridge import upsert_triple

        await upsert_triple(self.semantic, entry)
        triple = f"{entry.subject}|{entry.predicate}|{entry.object}"
        await self._emit_memory_op(
            operation="write",
            memory_id=getattr(entry, "id", None),
            type_summary="relational_triple",
            content_digest=_content_digest(triple),
            trigger="agent_relate",
        )

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

    async def _emit_memory_op(
        self,
        *,
        operation: str,
        memory_id: str | None = None,
        memory_ids: list[str] | None = None,
        type_summary: str | None = None,
        trust_tier: str | None = None,
        content_digest: str | None = None,
        trigger: str | None = None,
    ) -> None:
        """Best-effort memory_op emit. No-op when no emitter is wired.

        ``compact`` operation is not emitted from here — Loom has no
        compaction caller in v0.3.x; the dedicated compaction emit
        site lands when memory v2 introduces the merge primitive.
        """
        if self.ledger_emitter is None:
            return
        from loom.core.ledger import MemoryOpPayload

        try:
            await self.ledger_emitter.emit_memory_op(
                payload=MemoryOpPayload(
                    operation=operation,
                    memory_id=memory_id,
                    memory_ids=memory_ids,
                    type_summary=type_summary,
                    trust_tier=trust_tier,
                    content_digest=content_digest,
                    trigger=trigger,
                ),
            )
        except Exception:  # noqa: BLE001 — ledger must never break memory writes
            logger.exception("ledger memory_op emit failed; continuing")

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
