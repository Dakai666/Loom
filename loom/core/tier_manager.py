"""LLM tier state machine (Issue #276), extracted from ``LoomSession``.

``[cognition.tiers]`` in ``loom.toml`` maps an int tier → model name. Skills
declare ``model_tier: N`` in their frontmatter; activating one escalates the
active model to that tier (sticky). The agent or user can downgrade explicitly;
a ``/model`` selection is a manual override that wins until the tier changes
again.

The active model resolves with this precedence:

    manual ``/model`` override  >  sticky tier  >  default tier  >  base model

``TierManager`` is a pure state machine + event producer. It never touches
telemetry or the router — the owning ``LoomSession`` reacts to the returned
``TierChanged`` / ``TierExpiryHint`` events (refreshing ``runtime_identity``,
queueing reminders) and performs the actual model switch. The base model lives
on the session (``self._model``); ``TierManager`` reads it live through a
getter so there is a single source of truth.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Callable

from loom.core.events import TierChanged, TierExpiryHint


class TierManager:
    """Owns sticky-tier state and resolves the active model/tier."""

    def __init__(
        self,
        *,
        base_model_getter: Callable[[], str],
        tier_models: dict[int, str],
        default_tier: int,
        reminder_after_turns: int,
    ) -> None:
        self._base_model = base_model_getter
        self.tier_models = tier_models
        self.default_tier = default_tier
        self.reminder_after_turns = reminder_after_turns
        # ``None`` = follow ``default_tier``; integer = sticky override.
        self.sticky_tier: int | None = None
        self.manual_override: bool = False
        # Consecutive turns at the *active* tier (sticky or default). Reset on
        # tier change; surfaces in TierExpiryHint after the threshold.
        self.turns_at_current_tier: int = 0
        # "Did we already emit a reminder this sticky session?" — avoids
        # spamming the agent every turn once the threshold is crossed.
        self.reminder_emitted: bool = False
        # Skill name → declared tier, populated during skill bootstrap so
        # ``skill_max_tier`` resolves synchronously (procedural.get is async).
        self.skill_tier_snapshot: dict[str, int] = {}

    @classmethod
    def from_config(
        cls, config: dict, base_model_getter: Callable[[], str],
    ) -> "TierManager":
        """Build from the ``[cognition.tiers]`` table."""
        tier_cfg = (config.get("cognition") or {}).get("tiers", {}) or {}
        tier_models: dict[int, str] = {}
        for k, v in tier_cfg.items():
            try:
                tier_int = int(k)
                if isinstance(v, str) and v.strip():
                    tier_models[tier_int] = v.strip()
            except (TypeError, ValueError):
                continue
        return cls(
            base_model_getter=base_model_getter,
            tier_models=tier_models,
            default_tier=int(tier_cfg.get("default_tier", 1) or 1),
            reminder_after_turns=int(tier_cfg.get("reminder_after_turns", 10) or 10),
        )

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def active_tier(self) -> int:
        """Effective tier this turn — sticky override or default."""
        return self.sticky_tier if self.sticky_tier is not None else self.default_tier

    def active_model(self) -> str:
        """Resolve the model name for the active tier.

        A manual ``/model`` selection wins until tier changes again. Otherwise
        falls back to the base model when the tier system isn't configured for
        this tier.
        """
        base = self._base_model()
        if self.manual_override:
            return base
        return self.tier_models.get(self.active_tier(), base)

    # ------------------------------------------------------------------
    # Transitions (return events; caller owns the side effects)
    # ------------------------------------------------------------------

    def set_sticky(
        self, new_tier: int | None, *, reason: str, source: str,
    ) -> TierChanged | None:
        """Update sticky tier + reset counters. Returns a ``TierChanged`` when
        the active tier actually moved, else ``None``.

        ``source`` is one of ``"skill"`` / ``"agent"`` / ``"user"`` /
        ``"clear"`` and lands in the event for telemetry / graphify analysis.
        """
        base = self._base_model()
        old_tier = self.active_tier()
        # Normalize: setting sticky to default_tier is equivalent to clearing,
        # so the "no override" state remains canonical.
        if new_tier == self.default_tier:
            new_tier = None
        if new_tier == self.sticky_tier:
            return None  # no-op
        self.manual_override = False
        self.sticky_tier = new_tier
        self.turns_at_current_tier = 0
        self.reminder_emitted = False
        active = self.active_tier()
        if active == old_tier:
            return None
        return TierChanged(
            from_tier=old_tier,
            to_tier=active,
            from_model=self.tier_models.get(old_tier, base),
            to_model=self.tier_models.get(active, base),
            source=source,
            reason=reason,
        )

    def tick_counter(self) -> TierExpiryHint | None:
        """Increment the per-turn counter for the active tier and return a
        ``TierExpiryHint`` the first time the threshold is crossed within a
        sticky session.

        Called once per ``stream_turn`` completion. Skips emission when on the
        default tier, when the threshold isn't configured (``<= 0``), or when
        the hint already fired this sticky session.
        """
        self.turns_at_current_tier += 1
        if self.sticky_tier is None:
            return None
        if self.reminder_after_turns <= 0:
            return None
        if self.reminder_emitted:
            return None
        if self.turns_at_current_tier < self.reminder_after_turns:
            return None
        self.reminder_emitted = True
        return TierExpiryHint(
            tier=self.sticky_tier,
            model=self.active_model(),
            turns_used=self.turns_at_current_tier,
            threshold=self.reminder_after_turns,
        )

    def mark_manual_override(self) -> None:
        """Record that a manual ``/model`` selection now governs the model."""
        self.manual_override = True

    def skill_max_tier(self, activated_names: Iterable[str]) -> int:
        """Max ``model_tier`` across the currently-activated skills.

        Skills with no declared tier contribute 0, never escalating.
        """
        max_tier = 0
        for name in activated_names or ():
            tier = self.skill_tier_snapshot.get(name, 0)
            if tier > max_tier:
                max_tier = tier
        return max_tier


__all__ = ["TierManager"]
