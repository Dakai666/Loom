"""
Skill Gate — load-time routing between parent and shadow SKILL.md (Issue #120 PR 3).

When a ``load_skill`` call resolves a ``SkillGenome`` from ProceduralMemory,
the gate decides whether to serve the parent body or a shadow candidate that
is currently being trialled.  The decision is **deterministic per session**:
the same session always sees the same side of the shadow split for a given
skill, so TaskReflector can cleanly attribute quality outcomes.

Modes (``[mutation].shadow_mode`` in loom.toml):
- ``off``        — always serve parent. No candidate ever served.
- ``auto_c``     — mode C (default): candidates that reach ``shadow`` status
                   are split by ``shadow_fraction`` of sessions.
- ``manual_b``   — mode B: shadow candidates are only served when the user
                   (or an agent tool) explicitly requests the shadow side.

The gate is a pure lookup — it does not mutate any state.  The actual
transition ``generated → shadow`` is owned by ``SkillPromoter`` and happens
in the TaskReflector mutation post-hook (auto_c) or via the promote tool /
CLI (manual_b).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loom.core.memory.procedural import ProceduralMemory, SkillGenome

logger = logging.getLogger(__name__)


SHADOW_MODES: tuple[str, ...] = ("off", "auto_c", "manual_b")


@dataclass(frozen=True)
class GateDecision:
    """Resolved body + audit metadata for a single ``load_skill`` call."""

    body: str
    source: str                   # "parent" | "shadow"
    served_version: int           # parent.version for "parent"; parent.version for "shadow" (candidate is parent_version + 1 once promoted)
    candidate_id: str | None = None
    shadow_mode: str = "off"

    @property
    def is_shadow(self) -> bool:
        return self.source == "shadow"

    def audit_tag(self) -> str:
        """Compact string for logging / diagnostic metadata."""
        if self.source == "shadow":
            return f"shadow:{self.candidate_id[:8] if self.candidate_id else '?'}"
        return "parent"


class SkillGate:
    """Decides which SKILL.md body to serve on each ``load_skill``.

    Parameters
    ----------
    procedural:
        Memory layer used to look up shadow candidates on demand.
    shadow_mode:
        One of ``SHADOW_MODES``.  Invalid values fall back to ``"off"``.
    shadow_fraction:
        Fraction of sessions (0.0–1.0) that should see the shadow side in
        ``auto_c`` mode.  Clamped on the way in.
    session_id:
        Used to deterministically assign a session to a shadow slice.

    Notes
    -----
    The gate only *reads* ProceduralMemory.  Any lifecycle transition
    (``generated → shadow``, promote, rollback) is SkillPromoter's job;
    the gate sees the result of those transitions on the next call.
    """

    def __init__(
        self,
        procedural: "ProceduralMemory",
        shadow_mode: str = "auto_c",
        shadow_fraction: float = 0.5,
        session_id: str = "",
    ) -> None:
        self._procedural = procedural
        self._session_id = session_id or ""

        if shadow_mode not in SHADOW_MODES:
            logger.debug(
                "SkillGate: unknown shadow_mode %r — falling back to 'off'",
                shadow_mode,
            )
            shadow_mode = "off"
        self._shadow_mode = shadow_mode
        self._shadow_fraction = max(0.0, min(1.0, float(shadow_fraction)))

        # Per-session force overrides set via ``force_shadow`` / ``force_parent``
        # (mode B manual path, or CLI ``loom skill shadow --sticky``).  Keyed
        # by skill_name; value is the candidate_id for force_shadow, "" for
        # force_parent.
        self._overrides: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Configuration / introspection
    # ------------------------------------------------------------------

    @property
    def shadow_mode(self) -> str:
        return self._shadow_mode

    @property
    def shadow_fraction(self) -> float:
        return self._shadow_fraction

    def force_shadow(self, skill_name: str, candidate_id: str) -> None:
        """Pin this session to the shadow side for ``skill_name``."""
        self._overrides[skill_name] = candidate_id

    def force_parent(self, skill_name: str) -> None:
        """Pin this session to the parent body for ``skill_name``."""
        self._overrides[skill_name] = ""

    def clear_override(self, skill_name: str) -> None:
        self._overrides.pop(skill_name, None)

    # ------------------------------------------------------------------
    # Core decision
    # ------------------------------------------------------------------

    async def resolve(self, skill: "SkillGenome") -> GateDecision:
        """Return the body + audit metadata to serve for one ``load_skill`` call."""
        parent_decision = GateDecision(
            body=skill.body,
            source="parent",
            served_version=skill.version,
            candidate_id=None,
            shadow_mode=self._shadow_mode,
        )

        if self._shadow_mode == "off":
            return parent_decision

        # Explicit per-session override wins over slicing.
        if skill.name in self._overrides:
            override = self._overrides[skill.name]
            if not override:
                return parent_decision
            cand = await self._procedural.get_candidate(override)
            if cand is not None and cand.status == "shadow":
                return GateDecision(
                    body=cand.candidate_body,
                    source="shadow",
                    served_version=skill.version,
                    candidate_id=cand.id,
                    shadow_mode=self._shadow_mode,
                )
            # Override no longer valid — clear it and fall through.
            self._overrides.pop(skill.name, None)

        # Look up the freshest shadow candidate for this skill.
        candidates = await self._procedural.list_candidates(
            parent_skill_name=skill.name, status="shadow", limit=1,
        )
        if not candidates:
            return parent_decision
        shadow = candidates[0]

        if self._shadow_mode == "manual_b":
            # Mode B requires explicit opt-in; auto-slicing never fires.
            return parent_decision

        # Mode C — deterministic slicing.
        if not self._session_in_shadow_slice(skill.name):
            return parent_decision

        return GateDecision(
            body=shadow.candidate_body,
            source="shadow",
            served_version=skill.version,
            candidate_id=shadow.id,
            shadow_mode=self._shadow_mode,
        )

    # ------------------------------------------------------------------
    # Slicing
    # ------------------------------------------------------------------

    def _session_in_shadow_slice(self, skill_name: str) -> bool:
        """Deterministic yes/no based on (session_id, skill_name).

        Hashing keeps the assignment stable across reloads — the same
        session + skill always resolves the same way, which is critical
        for A/B comparisons in TaskReflector.
        """
        if self._shadow_fraction <= 0.0:
            return False
        if self._shadow_fraction >= 1.0:
            return True
        digest = hashlib.sha1(
            f"{self._session_id}|{skill_name}".encode("utf-8"),
        ).digest()
        # Use the first 4 bytes → int32, map to [0, 1).
        bucket = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
        return bucket < self._shadow_fraction


__all__ = ["SHADOW_MODES", "GateDecision", "SkillGate"]
