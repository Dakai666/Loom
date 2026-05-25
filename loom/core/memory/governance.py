"""
Memory Governor — unified governance layer over all memory types.

Issue #43: Advanced Memory Governance.

The Governor is always-on — instantiated in every LoomSession.start() and
wired into the memory write path.  It provides:

1. **Governed upsert** — trust-tier-adjusted writes with contradiction checks
2. **Admission gate** — filters candidate facts before episodic→semantic promotion
3. **Decay cycle** — automated TTL enforcement across all memory types

Governance events are logged to ``audit_log`` with ``tool_name`` prefixed
by ``governance:`` so they can be filtered alongside regular tool audit entries.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from typing import TYPE_CHECKING

from loom.core.memory.classifier import infer_domain
from loom.core.memory.contradiction import (
    ContradictionDetector,
    Resolution,
)
from loom.core.memory.health import MemoryHealthTracker
from loom.core.memory.ontology import DEFAULT_DOMAIN
from loom.core.memory.semantic import SemanticEntry, classify_source

if TYPE_CHECKING:
    import aiosqlite
    from loom.core.memory.episodic import EpisodicMemory
    from loom.core.memory.procedural import ProceduralMemory
    from loom.core.memory.pulse import MemoryPulse
    from loom.core.memory.semantic import SemanticMemory

logger = logging.getLogger(__name__)

# Module-level constant — avoid re-creating per call (PR #65 review)
_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be",
    "to", "of", "in", "on", "at", "for", "with", "by",
    "and", "or", "not", "it", "this", "that", "as",
    "的", "是", "了", "在", "有", "和", "就", "不",
    "也", "都", "而", "及", "與", "但", "或",
})


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class GovernedWriteResult:
    """Outcome of a governed upsert."""
    written: bool
    trust_tier: str
    adjusted_confidence: float
    contradictions_found: int
    resolution: str | None = None  # "replaced" | "superseded" | "kept" | None


@dataclass
class AdmissionResult:
    """Outcome of evaluating a single candidate fact for admission."""
    fact: str
    admitted: bool
    score: float        # 0.0–1.0 composite quality score
    reason: str         # "novel" | "duplicate" | "too_short" | "low_info"


@dataclass
class DecayCycleResult:
    """Summary of a periodic decay/prune cycle.

    Memory Lifecycle (issue #281 P2) introduces a two-stage transition:
    rows can be **archived** (demoted to second-chance state) before being
    **deleted**. ``*_pruned`` totals archived + deleted so legacy callers
    keep a single number; ``*_archived`` is exposed for richer reporting.
    """
    semantic_pruned: int
    episodic_pruned: int
    total_examined: int
    semantic_archived: int = 0
    # Issue #451 phase B: relational triples now live in semantic_entries
    # via the bridge — they decay alongside ordinary semantic facts. The
    # legacy ``relational_pruned`` / ``relational_archived`` counters are
    # retained as properties (always 0) so consumers that read them by
    # name don't crash mid-cutover.
    relational_pruned: int = 0
    relational_archived: int = 0

    @property
    def total_pruned(self) -> int:
        return self.semantic_pruned + self.episodic_pruned

    @property
    def total_archived(self) -> int:
        return self.semantic_archived


# ---------------------------------------------------------------------------
# Governor
# ---------------------------------------------------------------------------

class MemoryGovernor:
    """Always-on governance layer over all memory types.

    Injected into ``LoomSession`` at startup — no config toggle needed.

    Parameters
    ----------
    semantic:   SemanticMemory instance (since #451 phase B this also
                hosts relational triples via the bridge — no separate
                ``RelationalMemory`` parameter).
    procedural: ProceduralMemory instance
    episodic:   EpisodicMemory instance
    db:         Raw aiosqlite connection (for audit_log writes)
    config:     Governance config dict from loom.toml [memory.governance]
    """

    def __init__(
        self,
        semantic: SemanticMemory,
        procedural: ProceduralMemory,
        episodic: EpisodicMemory,
        db: aiosqlite.Connection,
        config: dict | None = None,
        session_id: str = "unknown",
    ) -> None:
        self._semantic = semantic
        self._procedural = procedural
        self._episodic = episodic
        self._db = db
        self._detector = ContradictionDetector(semantic)

        cfg = config or {}
        self._admission_threshold: float = cfg.get("admission_threshold", 0.5)
        self._episodic_ttl_days: int = cfg.get("episodic_ttl_days", 30)
        self._semantic_decay_threshold: float = cfg.get("semantic_decay_threshold", 0.1)
        # Issue #411: time-windowed novelty lookback. Old code used
        # ``list_recent(limit=50)`` — a position window that pushed yesterday's
        # facts out of comparison range within hours on a busy session, letting
        # the same idea sneak through gate after gate. Time window survives
        # bursty writes; the 500-row cap protects perf on bloated DBs.
        self._dup_window_days: int = cfg.get("dup_window_days", 7)
        self._dup_lookback_limit: int = cfg.get("dup_lookback_limit", 500)
        # Issue #411: embedding cosine threshold for semantic-similar duplicates.
        # 0.85 matches PR #409's manual-memorize hint default so manual + auto
        # paths agree on what "near-duplicate" means.
        self._dup_similarity_threshold: float = cfg.get(
            "dup_similarity_threshold", 0.85
        )
        # Issue #281 P3: lifecycle throttle. Cross-caller gate so daemon-cron
        # and session.stop() paths skip when the previous run was recent.
        # Read from [memory.lifecycle] in session wiring; 0.0 disables.
        self._lifecycle_min_gap_minutes: float = cfg.get("lifecycle_min_gap_minutes", 0.0)
        # Note: ``relational_decay_factor`` is no longer used — Memory
        # Lifecycle (issue #281 P2) computes per-domain half-lives directly.
        # Config key stays accepted (and ignored) for backward compatibility
        # with existing loom.toml entries.
        self._governance_log_write_warning_emitted = False

        # Issue #133: memory health tracking for agent self-observability
        self.health = MemoryHealthTracker(db, session_id=session_id)

        # Issue #281 P3 Hook A — late-bound by LoomSession.start() once the
        # MemoryPulse is constructed (depends on _pending_pulses buffer).
        # Optional: governor stays usable in tests / non-session contexts.
        self._pulse: "MemoryPulse | None" = None

    def set_pulse(self, pulse: "MemoryPulse | None") -> None:
        """Wire the MemoryPulse for Hook A contradiction notices."""
        self._pulse = pulse

    # ------------------------------------------------------------------
    # 1. Governed upsert
    # ------------------------------------------------------------------

    async def governed_upsert(self, entry: SemanticEntry) -> GovernedWriteResult:
        """Write a semantic entry through the governance pipeline.

        Steps:
        1. Classify source → assign trust tier
        2. Adjust confidence floor based on trust tier
        3. Run contradiction detection
        4. Auto-resolve any contradictions
        5. Write (or skip) based on resolution
        6. Log governance event to audit_log
        """
        tier_name, tier_confidence = classify_source(entry.source)

        # Ensure confidence is at least the trust tier's default
        # (but don't lower an explicitly-set high confidence)
        adjusted = max(entry.confidence, tier_confidence)
        entry.confidence = adjusted

        # Memory Ontology v0.1 (issue #281): if domain is the safe default,
        # try to upgrade it via the heuristic classifier. We only override
        # when the classifier finds a more specific axis — explicit
        # ``knowledge`` from the caller is preserved as-is.
        if entry.domain == DEFAULT_DOMAIN:
            inferred = infer_domain(entry.key)
            if inferred != DEFAULT_DOMAIN:
                entry.domain = inferred

        # Contradiction check
        contradictions = await self._detector.detect(entry)
        resolution_str: str | None = None

        if contradictions:
            # Process the most significant contradiction (highest similarity)
            main = max(contradictions, key=lambda c: c.similarity_score)
            result = self._detector.resolve(main)

            # Issue #281 P3 Hook A — fire once per (key × session) regardless
            # of resolution: even when the existing fact wins (KEEP), the
            # agent benefits from knowing a contradicting attempt happened.
            if self._pulse is not None:
                await self._pulse.contradiction_inject(main)

            if result.resolution == Resolution.KEEP:
                # Existing entry wins — don't write proposed
                await self._log_governance(
                    "governance:contradiction",
                    f"KEEP existing for key={entry.key}",
                    {
                        "trust_tier": tier_name,
                        "resolution": "keep",
                        "existing_source": main.existing.source,
                        "proposed_source": entry.source,
                        "reason": result.reason,
                    },
                )
                return GovernedWriteResult(
                    written=False,
                    trust_tier=tier_name,
                    adjusted_confidence=adjusted,
                    contradictions_found=len(contradictions),
                    resolution="kept",
                )

            resolution_str = result.resolution.value

        # Write through to semantic memory
        conflicted = await self._semantic.upsert(entry)
        if conflicted:
            resolution_str = resolution_str or "replaced"

        # Log successful governance write
        if contradictions:
            await self._log_governance(
                "governance:write",
                f"{resolution_str} for key={entry.key}",
                {
                    "trust_tier": tier_name,
                    "confidence": adjusted,
                    "contradictions": len(contradictions),
                    "resolution": resolution_str,
                },
            )

        return GovernedWriteResult(
            written=True,
            trust_tier=tier_name,
            adjusted_confidence=adjusted,
            contradictions_found=len(contradictions),
            resolution=resolution_str,
        )

    # ------------------------------------------------------------------
    # 2. Admission gate
    # ------------------------------------------------------------------

    async def evaluate_admission(
        self,
        candidate_facts: list[str],
        source: str,
    ) -> list[AdmissionResult]:
        """Score candidate facts before promotion to semantic memory.

        Scoring criteria (each 0.0–1.0, averaged):
        - **Length score**: too short (<15 chars) = low, optimal 30-300 = high
        - **Info density**: ratio of non-stopword tokens
        - **Novelty**: rejected when either the embedding cosine OR the lexical
          Jaccard exceeds threshold against any recent fact in the time window.

        Facts scoring >= ``admission_threshold`` (default 0.5) are admitted.

        Issue #411: novelty now combines embedding cosine (catches paraphrase)
        with lexical Jaccard (catches near-exact + acts as fallback when the
        embedding provider is unavailable). Lookback is a time window so
        yesterday's facts are still comparable today.
        """
        results: list[AdmissionResult] = []

        # Pre-fetch recent facts for lexical novelty check.
        cutoff = datetime.now(UTC) - timedelta(days=self._dup_window_days)
        recent_facts = await self._semantic.list_between(
            since=cutoff, limit=self._dup_lookback_limit,
        )
        recent_values = [f.value.lower() for f in recent_facts]

        for fact in candidate_facts:
            # Embedding-based semantic dup check — silently skipped when the
            # embedding provider is missing or fails (find_near_duplicates
            # returns [] in either case, so lexical path still runs).
            semantic_dup = False
            try:
                near = await self._semantic.find_near_duplicates(
                    fact,
                    min_similarity=self._dup_similarity_threshold,
                    within_days=self._dup_window_days,
                    limit=1,
                )
                semantic_dup = bool(near)
            except Exception:
                pass

            score, reason = self._score_fact(
                fact, recent_values, semantic_dup=semantic_dup,
            )
            admitted = score >= self._admission_threshold
            results.append(AdmissionResult(
                fact=fact,
                admitted=admitted,
                score=round(score, 3),
                reason=reason,
            ))

        # Log admission summary
        admitted_count = sum(1 for r in results if r.admitted)
        rejected_count = len(results) - admitted_count
        if rejected_count > 0:
            await self._log_governance(
                "governance:admission",
                f"Admitted {admitted_count}/{len(results)} facts",
                {
                    "total": len(results),
                    "admitted": admitted_count,
                    "rejected": rejected_count,
                    "threshold": self._admission_threshold,
                    "source": source,
                },
            )

        return results

    def _score_fact(
        self,
        fact: str,
        recent_values: list[str],
        *,
        semantic_dup: bool = False,
    ) -> tuple[float, str]:
        """Score a single candidate fact. Returns (score, reason).

        ``semantic_dup`` is computed by the caller via embedding cosine and
        OR-combined with the lexical check below — either one tripping is
        sufficient to mark the fact as duplicate.
        """
        scores: dict[str, float] = {}

        # ── Length score ────────────────────────────────────────────────
        length = len(fact.strip())
        if length < 10:
            return 0.1, "too_short"
        elif length < 20:
            scores["length"] = 0.3
        elif length <= 500:
            scores["length"] = 0.8
        else:
            scores["length"] = 0.5  # very long facts are still OK

        # ── Info density ───────────────────────────────────────────────
        words = fact.lower().split()
        if not words:
            return 0.1, "empty"

        # _STOPWORDS defined at module level
        info_words = [w for w in words if w not in _STOPWORDS and len(w) > 1]
        scores["info_density"] = min(1.0, len(info_words) / max(len(words), 1))

        # ── Novelty: semantic OR lexical (Issue #411) ──────────────────
        if semantic_dup:
            return 0.2, "duplicate_semantic"

        fact_lower = fact.lower()
        max_overlap = 0.0
        for existing in recent_values:
            overlap = _word_overlap(fact_lower, existing)
            max_overlap = max(max_overlap, overlap)

        if max_overlap > 0.8:
            return 0.2, "duplicate"
        scores["novelty"] = 1.0 - max_overlap

        # ── Composite score ────────────────────────────────────────────
        composite = (
            scores.get("length", 0.5) * 0.2
            + scores.get("info_density", 0.5) * 0.3
            + scores.get("novelty", 0.5) * 0.5
        )

        reason = "novel" if composite >= self._admission_threshold else "low_quality"
        return composite, reason

    # ------------------------------------------------------------------
    # 3. Decay cycle
    # ------------------------------------------------------------------

    async def run_decay_cycle(self) -> DecayCycleResult:
        """Execute periodic decay across all memory types.

        Called at session shutdown (after compression). Memory Lifecycle
        (issue #281 P2) drives the semantic + relational paths via a single
        ``(domain, temporal)`` half-life table; episodic TTL stays here
        because it's a flat age cutoff, not effective_confidence.

        Fixes issue #299 — relational triples from every source are now
        examined, not just ``source='dreaming'``.
        """
        from loom.core.memory.lifecycle import MemoryLifecycle

        cycle = MemoryLifecycle(self._db, threshold=self._semantic_decay_threshold)
        lifecycle_result = await cycle.run(
            min_gap_minutes=self._lifecycle_min_gap_minutes,
        )

        # ── Episodic TTL (still a flat age check, not lifecycle-managed) ──
        episodic_pruned = await self._prune_episodic_ttl()

        # #451 phase B: rel:* triples live in semantic_entries, so the
        # semantic decay path covers them. Lifecycle's relational counters
        # are 0 for fresh installs but may report > 0 on legacy DBs during
        # the one-shot migration cycle before the table is dropped.
        result = DecayCycleResult(
            semantic_pruned=(
                lifecycle_result.semantic_archived + lifecycle_result.semantic_deleted
            ),
            episodic_pruned=episodic_pruned,
            total_examined=lifecycle_result.semantic_examined,
            semantic_archived=lifecycle_result.semantic_archived,
        )

        if result.total_pruned > 0:
            await self._log_governance(
                "governance:decay",
                f"Pruned {result.total_pruned} entries",
                {
                    "semantic_archived": lifecycle_result.semantic_archived,
                    "semantic_deleted":  lifecycle_result.semantic_deleted,
                    "episodic": episodic_pruned,
                    "examined": result.total_examined,
                },
            )

        return result

    async def _prune_episodic_ttl(self) -> int:
        """Delete episodic entries older than TTL days."""
        cutoff = datetime.now(UTC) - timedelta(days=self._episodic_ttl_days)
        cutoff_iso = cutoff.isoformat()

        try:
            cursor = await self._db.execute(
                "DELETE FROM episodic_entries WHERE created_at < ?",
                (cutoff_iso,),
            )
            await self._db.commit()
            return cursor.rowcount or 0
        except Exception as exc:
            logger.debug("Episodic TTL prune failed: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # Audit logging
    # ------------------------------------------------------------------

    async def _log_governance(
        self, tool_name: str, error_or_note: str, details: dict
    ) -> None:
        """Write a governance event to the existing audit_log table.

        Reuses audit_log fields:
        - tool_name: "governance:<event_type>"
        - trust_level: "GOVERNANCE"
        - success: 1 (always — governance events are informational)
        - error: human-readable note
        - details: JSON dict with structured event data
        """
        try:
            await self._db.execute(
                """
                INSERT INTO audit_log
                    (id, session_id, tool_name, trust_level, success,
                     duration_ms, error, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    "governance",  # synthetic session_id
                    tool_name,
                    "GOVERNANCE",
                    1,
                    0.0,
                    error_or_note,
                    json.dumps(details, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                ),
            )
            await self._db.commit()
        except Exception as exc:
            if not self._governance_log_write_warning_emitted:
                logger.warning("Governance audit log write failed: %s", exc)
                self._governance_log_write_warning_emitted = True
            else:
                logger.debug("Governance audit log write failed: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _word_overlap(a: str, b: str) -> float:
    """Word-level Jaccard overlap (cheap duplicate check)."""
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)
