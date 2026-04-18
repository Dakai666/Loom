"""
Skill Promoter — the lifecycle engine for Issue #120 PR 3.

Turns ``SkillCandidate`` rows from the ``generated`` / ``shadow`` states into
the live SKILL.md served by the session:

    generated ──(auto_shadow)──► shadow ──(promote)──► promoted
        │                           │
        └──────── (deprecate)──────▶  deprecated

Rollback restores a previously-promoted body from ``skill_version_history``
and marks the candidate that had been serving as ``rolled_back``.

Design notes
------------
- The promoter is a **pure state-machine over ProceduralMemory**; it does not
  touch SKILL.md files on disk.  PR 3's on-disk archive (``~/.loom/skills/
  .history/``) is handled by a thin adapter in Session so the core logic stays
  testable without a filesystem.
- ``PromotionEvent`` is broadcast to subscribers after every successful
  transition so CLI / TUI / Discord can surface the change.
- All transitions are idempotent at the status level: attempting to promote
  a candidate that is already ``promoted`` is a no-op that returns the
  existing skill (logged at debug).  Illegal transitions raise ``ValueError``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import TYPE_CHECKING, Awaitable, Callable

from loom.core.memory.procedural import (
    CANDIDATE_STATUSES,
    SkillCandidate,
    SkillGenome,
    SkillVersionRecord,
)

if TYPE_CHECKING:
    from loom.core.memory.procedural import ProceduralMemory

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------

PROMOTION_EVENT_KINDS: tuple[str, ...] = (
    "auto_shadow",
    "promote",
    "rollback",
    "deprecate",
)


@dataclass
class PromotionEvent:
    """Announcement of a lifecycle transition on a skill.

    Emitted after ProceduralMemory has been updated — subscribers see the
    post-transition state.  The skill body is intentionally omitted to keep
    notification payloads small; subscribers fetch on demand if they need it.
    """

    kind: str                              # see PROMOTION_EVENT_KINDS
    skill_name: str
    candidate_id: str | None
    from_version: int | None
    to_version: int
    reason: str | None = None
    session_id: str | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.kind not in PROMOTION_EVENT_KINDS:
            raise ValueError(
                f"Invalid promotion event kind {self.kind!r}; "
                f"expected one of {PROMOTION_EVENT_KINDS}"
            )

    def one_line_summary(self) -> str:
        tail = f" ({self.reason})" if self.reason else ""
        if self.kind == "rollback":
            return (
                f"{self.skill_name}: rollback "
                f"v{self.from_version}→v{self.to_version}{tail}"
            )
        if self.kind == "deprecate":
            return f"{self.skill_name}: deprecated candidate {self.candidate_id}{tail}"
        if self.kind == "auto_shadow":
            return f"{self.skill_name}: candidate → shadow{tail}"
        # promote
        return f"{self.skill_name}: promoted v{self.from_version}→v{self.to_version}{tail}"


PromotionSubscriber = Callable[[PromotionEvent], Awaitable[None]]


# ---------------------------------------------------------------------------
# Promoter
# ---------------------------------------------------------------------------


class SkillPromoter:
    """Lifecycle engine for ``SkillCandidate`` rows.

    Parameters
    ----------
    procedural:
        The memory layer that owns ``skill_genomes``, ``skill_candidates``
        and ``skill_version_history``.
    session_id:
        Propagated onto every PromotionEvent so subscribers can correlate.
    """

    def __init__(
        self,
        procedural: "ProceduralMemory",
        session_id: str | None = None,
        shadow_mode: str = "auto_c",
        auto_shadow_confidence_ceiling: float = 0.7,
    ) -> None:
        self._procedural = procedural
        self._session_id = session_id
        self._shadow_mode = shadow_mode
        self._auto_shadow_confidence_ceiling = max(
            0.0, min(1.0, float(auto_shadow_confidence_ceiling))
        )
        self._subscribers: list[PromotionSubscriber] = []

    # ------------------------------------------------------------------
    # Configuration getters
    # ------------------------------------------------------------------

    @property
    def shadow_mode(self) -> str:
        return self._shadow_mode

    # ------------------------------------------------------------------
    # Subscribers
    # ------------------------------------------------------------------

    def subscribe(self, callback: PromotionSubscriber) -> None:
        """Register a coroutine that receives each ``PromotionEvent``."""
        self._subscribers.append(callback)

    async def _broadcast(self, event: PromotionEvent) -> None:
        for cb in list(self._subscribers):
            try:
                await cb(event)
            except Exception as exc:
                logger.debug("Promotion subscriber failed: %s", exc)

    # ------------------------------------------------------------------
    # Shadow transition (PR 3: mode C auto, mode B manual)
    # ------------------------------------------------------------------

    async def shadow(
        self,
        candidate_id: str,
        reason: str | None = None,
    ) -> SkillCandidate | None:
        """Move a ``generated`` candidate to ``shadow``.

        Idempotent: if the candidate is already ``shadow`` the call returns
        the existing row.  Raises ``ValueError`` if the candidate is in a
        terminal state (promoted / deprecated / rolled_back).
        """
        candidate = await self._procedural.get_candidate(candidate_id)
        if candidate is None:
            return None

        if candidate.status == "shadow":
            return candidate
        if candidate.status != "generated":
            raise ValueError(
                f"Cannot shadow candidate in status {candidate.status!r}; "
                f"only 'generated' candidates can move to 'shadow'."
            )

        updated_notes = _append_note(candidate.notes, f"shadow: {reason}" if reason else "shadow")
        await self._procedural.update_candidate_status(
            candidate_id, "shadow", notes=updated_notes,
        )
        refreshed = await self._procedural.get_candidate(candidate_id)

        await self._broadcast(PromotionEvent(
            kind="auto_shadow",
            skill_name=candidate.parent_skill_name,
            candidate_id=candidate.id,
            from_version=candidate.parent_version,
            to_version=candidate.parent_version,  # parent untouched in shadow
            reason=reason,
            session_id=self._session_id,
        ))
        return refreshed

    async def maybe_auto_shadow(
        self,
        candidate_id: str,
        reason: str | None = None,
    ) -> SkillCandidate | None:
        """Auto-transition ``generated → shadow`` when mode + parent allow it.

        Fires only when:
          - ``shadow_mode`` is ``"auto_c"``
          - the candidate exists and is still in ``generated`` status
          - the parent's EMA confidence is at or below
            ``auto_shadow_confidence_ceiling`` (i.e. the parent is actually
            underperforming enough to warrant an A/B trial)

        Returns the updated candidate on transition, ``None`` otherwise.
        Mode ``manual_b`` and ``off`` are handled by ignoring this call —
        the candidate stays ``generated`` until explicit promotion.
        """
        if self._shadow_mode != "auto_c":
            return None

        candidate = await self._procedural.get_candidate(candidate_id)
        if candidate is None or candidate.status != "generated":
            return None

        parent = await self._procedural.get(candidate.parent_skill_name)
        if parent is None:
            return None
        if parent.confidence > self._auto_shadow_confidence_ceiling:
            return None

        return await self.shadow(candidate_id, reason=reason or "auto")

    # ------------------------------------------------------------------
    # Promotion
    # ------------------------------------------------------------------

    async def promote(
        self,
        candidate_id: str,
        reason: str | None = None,
    ) -> SkillGenome | None:
        """Swap the parent SKILL.md for the candidate body.

        Steps, in order:
          1. Fetch candidate + parent; refuse if either is missing.
          2. Archive the current parent body to ``skill_version_history``
             with ``reason='promote'`` so rollback has a target.
          3. Overwrite ``SkillGenome.body`` with ``candidate_body`` and
             increment ``version``.  ``confidence`` / ``success_rate`` are
             **reset** to the skill's own starting defaults so the new body
             earns its own track record.
          4. Mark the candidate ``promoted``; mark any other candidates for
             the same parent that are still ``shadow`` as ``deprecated``
             (they were shadowing a body that no longer exists).
          5. Broadcast a PromotionEvent.

        Returns the updated ``SkillGenome``, or ``None`` if the candidate or
        parent could not be resolved.
        """
        candidate = await self._procedural.get_candidate(candidate_id)
        if candidate is None:
            logger.debug("SkillPromoter.promote: candidate %s not found", candidate_id)
            return None
        if candidate.status == "promoted":
            # Idempotent — return the current parent.
            return await self._procedural.get(candidate.parent_skill_name)
        if candidate.status not in ("generated", "shadow"):
            raise ValueError(
                f"Cannot promote candidate in status {candidate.status!r}; "
                f"only 'generated' or 'shadow' are promotable."
            )

        parent = await self._procedural.get(candidate.parent_skill_name)
        if parent is None:
            logger.debug(
                "SkillPromoter.promote: parent %s missing for candidate %s",
                candidate.parent_skill_name, candidate_id,
            )
            return None

        # (2) Archive current parent before overwrite.
        await self._procedural.archive_version(SkillVersionRecord(
            skill_name=parent.name,
            version=parent.version,
            body=parent.body,
            reason="promote",
            source_candidate_id=candidate.id,
        ))

        # (3) Swap + bump version + reset confidence track.
        from_version = parent.version
        parent.body = candidate.candidate_body
        parent.version = from_version + 1
        parent.confidence = 1.0
        parent.success_rate = 1.0
        parent.usage_count = 0
        parent.updated_at = datetime.now(UTC)
        await self._procedural.upsert(parent)

        # (4a) Mark candidate promoted.
        updated_notes = _append_note(candidate.notes, f"promote: {reason}" if reason else "promote")
        await self._procedural.update_candidate_status(
            candidate.id, "promoted", notes=updated_notes,
        )

        # (4b) Deprecate sibling shadows (stale — they'd shadow the old body).
        siblings = await self._procedural.list_candidates(
            parent_skill_name=parent.name, status="shadow",
        )
        for sib in siblings:
            if sib.id == candidate.id:
                continue
            await self._procedural.update_candidate_status(
                sib.id, "deprecated",
                notes=_append_note(sib.notes, f"superseded by {candidate.id}"),
            )

        # (5) Announce.
        await self._broadcast(PromotionEvent(
            kind="promote",
            skill_name=parent.name,
            candidate_id=candidate.id,
            from_version=from_version,
            to_version=parent.version,
            reason=reason,
            session_id=self._session_id,
        ))
        return parent

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    async def rollback(
        self,
        skill_name: str,
        to_version: int | None = None,
        reason: str | None = None,
    ) -> SkillGenome | None:
        """Restore a previous SKILL.md body from ``skill_version_history``.

        ``to_version=None`` rolls back to the **most recently archived**
        version (i.e. undo the latest promote).  Otherwise the specific
        version is fetched.  The current body is archived under
        ``reason='rollback'`` before the swap so the rollback itself is
        reversible.  Any candidate that had been ``promoted`` into the
        current version is marked ``rolled_back``.
        """
        parent = await self._procedural.get(skill_name)
        if parent is None:
            logger.debug("SkillPromoter.rollback: skill %s not found", skill_name)
            return None

        target: SkillVersionRecord | None
        if to_version is None:
            target = await self._procedural.latest_history(skill_name)
        else:
            target = await self._procedural.get_history_version(skill_name, to_version)
        if target is None:
            logger.debug(
                "SkillPromoter.rollback: no history entry for %s%s",
                skill_name, f" v{to_version}" if to_version else "",
            )
            return None

        # Archive current body before swapping it out (in case the operator
        # wants to roll the rollback back).
        await self._procedural.archive_version(SkillVersionRecord(
            skill_name=parent.name,
            version=parent.version,
            body=parent.body,
            reason="rollback",
            source_candidate_id=None,
        ))

        # Swap body; bump version so the audit trail stays monotonic even
        # though the body is logically regressing.  Reset confidence track.
        from_version = parent.version
        parent.body = target.body
        parent.version = from_version + 1
        parent.confidence = 1.0
        parent.success_rate = 1.0
        parent.usage_count = 0
        parent.updated_at = datetime.now(UTC)
        await self._procedural.upsert(parent)

        # Mark the most recent promoted candidate as rolled_back so the audit
        # chain connects: a candidate was promoted, then reverted.
        promoted = await self._procedural.list_candidates(
            parent_skill_name=skill_name, status="promoted", limit=1,
        )
        if promoted:
            top = promoted[0]
            await self._procedural.update_candidate_status(
                top.id, "rolled_back",
                notes=_append_note(top.notes, f"rolled back: {reason}" if reason else "rolled back"),
            )

        await self._broadcast(PromotionEvent(
            kind="rollback",
            skill_name=parent.name,
            candidate_id=promoted[0].id if promoted else None,
            from_version=from_version,
            to_version=parent.version,
            reason=reason,
            session_id=self._session_id,
        ))
        return parent

    # ------------------------------------------------------------------
    # Deprecation (reject without promotion)
    # ------------------------------------------------------------------

    async def deprecate(
        self,
        candidate_id: str,
        reason: str | None = None,
    ) -> SkillCandidate | None:
        """Mark a candidate ``deprecated`` without touching the parent."""
        candidate = await self._procedural.get_candidate(candidate_id)
        if candidate is None:
            return None
        if candidate.status == "deprecated":
            return candidate
        if candidate.status not in ("generated", "shadow"):
            raise ValueError(
                f"Cannot deprecate candidate in status {candidate.status!r}."
            )

        await self._procedural.update_candidate_status(
            candidate.id, "deprecated",
            notes=_append_note(candidate.notes, f"deprecated: {reason}" if reason else "deprecated"),
        )
        refreshed = await self._procedural.get_candidate(candidate.id)

        await self._broadcast(PromotionEvent(
            kind="deprecate",
            skill_name=candidate.parent_skill_name,
            candidate_id=candidate.id,
            from_version=candidate.parent_version,
            to_version=candidate.parent_version,
            reason=reason,
            session_id=self._session_id,
        ))
        return refreshed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _append_note(existing: str | None, addition: str) -> str:
    if not existing:
        return addition
    return f"{existing}; {addition}"


# Re-export so callers only import from this module.
__all__ = [
    "CANDIDATE_STATUSES",
    "PROMOTION_EVENT_KINDS",
    "PromotionEvent",
    "PromotionSubscriber",
    "SkillPromoter",
]
