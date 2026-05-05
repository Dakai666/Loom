"""
MemoryPulse — proactive memory hooks (Issue #281 P3, ontology §5).

Two hooks (G + A) that emit short text briefs into a session-scoped buffer
(``LoomSession._pending_pulses``), drained at the top of ``stream_turn``
alongside judge verdicts (#196 Phase 2 pattern). Pulses surface as
``<system-reminder>`` blocks on the agent's first LLM call of the next turn.

Hook G — Session preheat
    Once per ``start()``, query the previous active session's
    ``domain=project AND temporal=milestone`` facts, top-3 by confidence.
    Continuity across session boundaries without polluting context: ``None``
    when nothing relevant exists.

Hook A — Contradiction notice
    Each ``governed_upsert`` that detects a contradiction emits a short
    old-vs-new diff. Once-per-key gate via ``memory_meta`` so a hot key
    being rewritten repeatedly doesn't spam the agent — gate clears at
    session start (next session sees the contradiction once again if the
    issue is still live).

The remaining four hooks from #280 (E/H/B/D) are deferred until G+A
have run two weeks and noise is measurable.
"""

from __future__ import annotations

import logging
import textwrap
from datetime import datetime, UTC
from typing import TYPE_CHECKING

from loom.core.memory.ontology import (
    DOMAIN_PROJECT,
    TEMPORAL_MILESTONE,
)

if TYPE_CHECKING:
    import aiosqlite
    from loom.core.memory.contradiction import Contradiction
    from loom.core.memory.semantic import SemanticMemory

logger = logging.getLogger(__name__)


_BRIEF_TOP_N = 3
_GATE_KEY_PREFIX = "pulse.contradiction."


class MemoryPulse:
    """Owns the two active hooks (G + A). Stateless across calls — all
    persistence goes through ``memory_meta`` or the pending buffer."""

    def __init__(
        self,
        db: "aiosqlite.Connection",
        semantic: "SemanticMemory",
        session_id: str,
        session_started_at: datetime,
        pending_buffer: list[str],
    ) -> None:
        self._db = db
        self._semantic = semantic
        self._session_id = session_id
        self._session_started_at = session_started_at
        self._buffer = pending_buffer

    # ------------------------------------------------------------------
    # Hook G — Session preheat
    # ------------------------------------------------------------------

    async def session_brief(self) -> None:
        """Append a brief summarising the previous session's milestone-class
        project facts (top-3 by confidence). No-op when:
          - no prior session exists
          - no project/milestone fact has been touched since the prior
            session started (i.e. nothing the agent recently anchored on)
        """
        try:
            prev = await self._previous_active_session()
            if prev is None:
                return
            _, prev_started_at = prev

            # COALESCE: fall back to updated_at for facts never recalled
            # (last_accessed_at is NULL) — a fact written but never read in
            # the prior session is still relevant continuity.
            cursor = await self._db.execute(
                "SELECT key, value, confidence FROM semantic_entries "
                "WHERE domain = ? AND temporal = ? "
                "AND COALESCE(last_accessed_at, updated_at) >= ? "
                "ORDER BY confidence DESC LIMIT ?",
                (DOMAIN_PROJECT, TEMPORAL_MILESTONE,
                 prev_started_at.isoformat(), _BRIEF_TOP_N),
            )
            rows = await cursor.fetchall()
            if not rows:
                return

            lines = [f"- {row[0]}: {self._truncate(row[1])}" for row in rows]
            brief = (
                "Memory preheat — milestone facts active in your previous "
                "session (top by confidence):\n" + "\n".join(lines)
            )
            self._buffer.append(brief)
        except Exception as exc:
            logger.debug("pulse: session_brief failed: %s", exc)

    async def _previous_active_session(self) -> tuple[str, datetime] | None:
        cursor = await self._db.execute(
            "SELECT session_id, started_at FROM sessions "
            "WHERE session_id != ? ORDER BY last_active DESC LIMIT 1",
            (self._session_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        try:
            return row[0], datetime.fromisoformat(row[1])
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Hook A — Contradiction notice
    # ------------------------------------------------------------------

    async def contradiction_inject(self, contradiction: "Contradiction") -> None:
        """Append a once-per-key contradiction notice. Gate uses
        ``memory_meta`` keyed by the canonical fact key — if the same key
        contradicted earlier *this session*, skip.
        """
        try:
            key = contradiction.proposed.key
            if await self._already_notified_this_session(key):
                return

            old = self._truncate(contradiction.existing.value)
            new = self._truncate(contradiction.proposed.value)
            notice = (
                f"Memory contradiction on key={key!r} (resolution: "
                f"{contradiction.resolution.value if contradiction.resolution else 'pending'}):\n"
                f"  existing: {old}\n"
                f"  proposed: {new}\n"
                f"Reconcile if the new value should override the old."
            )
            self._buffer.append(notice)
            await self._mark_notified(key)
        except Exception as exc:
            logger.debug("pulse: contradiction_inject failed: %s", exc)

    async def _already_notified_this_session(self, key: str) -> bool:
        cursor = await self._db.execute(
            "SELECT updated_at FROM memory_meta WHERE key = ?",
            (_GATE_KEY_PREFIX + key,),
        )
        row = await cursor.fetchone()
        if not row:
            return False
        try:
            ts = datetime.fromisoformat(row[0])
        except ValueError:
            return False
        return ts >= self._session_started_at

    async def _mark_notified(self, key: str) -> None:
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "INSERT INTO memory_meta(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (_GATE_KEY_PREFIX + key, self._session_id, now),
        )
        # Deliberate early commit: gate persistence must survive the rest
        # of governed_upsert's path so a re-entrant write within the same
        # turn doesn't bypass the once-per-(key × session) check. Caller
        # (governance.governed_upsert) holds no other un-committed writes
        # at this point — do not introduce any before this hook fires.
        await self._db.commit()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _truncate(value: str, limit: int = 160) -> str:
        # textwrap.shorten is whitespace-aware so CJK / mixed-script values
        # don't get sliced mid-grapheme on the byte boundary. Falls back to
        # raw value if already short, since shorten() collapses whitespace.
        v = value.strip().replace("\n", " ")
        if len(v) <= limit:
            return v
        return textwrap.shorten(v, width=limit, placeholder="…")
