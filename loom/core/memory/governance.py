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

from loom.core.memory.contradiction import (
    ContradictionDetector,
    Resolution,
)
from loom.core.memory.semantic import SemanticEntry, classify_source

if TYPE_CHECKING:
    import aiosqlite
    from loom.core.memory.episodic import EpisodicMemory
    from loom.core.memory.procedural import ProceduralMemory
    from loom.core.memory.relational import RelationalMemory
    from loom.core.memory.semantic import SemanticMemory

logger = logging.getLogger(__name__)


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
    """Summary of a periodic decay/prune cycle."""
    semantic_pruned: int
    episodic_pruned: int
    relational_pruned: int
    total_examined: int

    @property
    def total_pruned(self) -> int:
        return self.semantic_pruned + self.episodic_pruned + self.relational_pruned


# ---------------------------------------------------------------------------
# Governor
# ---------------------------------------------------------------------------

class MemoryGovernor:
    """Always-on governance layer over all memory types.

    Injected into ``LoomSession`` at startup — no config toggle needed.

    Parameters
    ----------
    semantic:   SemanticMemory instance
    procedural: ProceduralMemory instance
    relational: RelationalMemory instance
    episodic:   EpisodicMemory instance
    db:         Raw aiosqlite connection (for audit_log writes)
    config:     Governance config dict from loom.toml [memory.governance]
    """

    def __init__(
        self,
        semantic: SemanticMemory,
        procedural: ProceduralMemory,
        relational: RelationalMemory,
        episodic: EpisodicMemory,
        db: aiosqlite.Connection,
        config: dict | None = None,
    ) -> None:
        self._semantic = semantic
        self._procedural = procedural
        self._relational = relational
        self._episodic = episodic
        self._db = db
        self._detector = ContradictionDetector(semantic)

        cfg = config or {}
        self._admission_threshold: float = cfg.get("admission_threshold", 0.5)
        self._episodic_ttl_days: int = cfg.get("episodic_ttl_days", 30)
        self._semantic_decay_threshold: float = cfg.get("semantic_decay_threshold", 0.1)
        self._relational_decay_factor: float = cfg.get("relational_decay_factor", 1.5)

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

        # Ensure confidence is at least the trust tier floor
        # (but don't lower an explicitly-set high confidence)
        adjusted = max(entry.confidence, tier_confidence * 0.5)
        entry.confidence = adjusted

        # Contradiction check
        contradictions = await self._detector.detect(entry)
        resolution_str: str | None = None

        if contradictions:
            # Process the most significant contradiction (highest similarity)
            main = max(contradictions, key=lambda c: c.similarity_score)
            result = self._detector.resolve(main)

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
        - **Novelty**: inverse of max similarity to existing recent facts

        Facts scoring >= ``admission_threshold`` (default 0.5) are admitted.
        """
        results: list[AdmissionResult] = []

        # Pre-fetch recent facts for novelty check
        recent_facts = await self._semantic.list_recent(limit=50)
        recent_values = [f.value.lower() for f in recent_facts]

        for fact in candidate_facts:
            score, reason = self._score_fact(fact, recent_values)
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
    ) -> tuple[float, str]:
        """Score a single candidate fact. Returns (score, reason)."""
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

        _STOPWORDS = frozenset({
            "the", "a", "an", "is", "are", "was", "were", "be",
            "to", "of", "in", "on", "at", "for", "with", "by",
            "and", "or", "not", "it", "this", "that", "as",
            "的", "是", "了", "在", "有", "和", "就", "不",
            "也", "都", "而", "及", "與", "但", "或",
        })
        info_words = [w for w in words if w not in _STOPWORDS and len(w) > 1]
        scores["info_density"] = min(1.0, len(info_words) / max(len(words), 1))

        # ── Novelty (inverse max similarity to recent facts) ───────────
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

        Called at session shutdown (after compression).

        - **Semantic**: prune entries whose effective_confidence < threshold
        - **Episodic**: delete entries older than TTL days
        - **Relational**: decay dreaming-sourced triples with higher factor
        """
        total_examined = 0

        # ── Semantic decay ──────────────────────────────────────────────
        sem_result = await self._semantic.prune_decayed(
            threshold=self._semantic_decay_threshold,
        )
        semantic_pruned = sem_result.get("pruned", 0)
        total_examined += sem_result.get("examined", 0)

        # ── Episodic TTL ────────────────────────────────────────────────
        episodic_pruned = await self._prune_episodic_ttl()

        # ── Relational decay ────────────────────────────────────────────
        relational_pruned = await self._prune_relational_decay()

        result = DecayCycleResult(
            semantic_pruned=semantic_pruned,
            episodic_pruned=episodic_pruned,
            relational_pruned=relational_pruned,
            total_examined=total_examined,
        )

        if result.total_pruned > 0:
            await self._log_governance(
                "governance:decay",
                f"Pruned {result.total_pruned} entries",
                {
                    "semantic": semantic_pruned,
                    "episodic": episodic_pruned,
                    "relational": relational_pruned,
                    "examined": total_examined,
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

    async def _prune_relational_decay(self) -> int:
        """Prune dreaming-sourced relational triples with accelerated decay.

        Dreaming triples use a shorter effective half-life:
        base_half_life / relational_decay_factor.

        Only triples with source='dreaming' are subject to accelerated decay.
        """
        import math

        base_half_life = 90.0  # days
        effective_half_life = base_half_life / self._relational_decay_factor

        cursor = await self._db.execute(
            "SELECT id, confidence, updated_at FROM relational_entries "
            "WHERE source = 'dreaming'"
        )
        rows = await cursor.fetchall()

        to_prune: list[str] = []
        for row_id, confidence, updated_at_str in rows:
            try:
                updated_at = datetime.fromisoformat(updated_at_str)
                days = (datetime.now(UTC) - updated_at).total_seconds() / 86400.0
                decayed = confidence * math.pow(2.0, -days / effective_half_life)
                if decayed < self._semantic_decay_threshold:
                    to_prune.append(row_id)
            except Exception:
                continue

        if to_prune:
            placeholders = ",".join("?" * len(to_prune))
            await self._db.execute(
                f"DELETE FROM relational_entries WHERE id IN ({placeholders})",
                to_prune,
            )
            await self._db.commit()

        return len(to_prune)

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
