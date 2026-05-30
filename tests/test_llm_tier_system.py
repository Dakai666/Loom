"""
Tests for the LLM tier system (Issue #276).

Covers:
  - Frontmatter parsing extracts ``model_tier``
  - SkillGenome stores ``model_tier``
  - LoomSession state: ``_active_tier``, ``_active_model``
  - ``_set_sticky_tier`` semantics + TierChanged event emission
  - ``_compute_skill_max_tier`` consults snapshot
  - ``_tick_tier_counter`` reminder threshold + once-per-session emission
  - ``request_model_tier`` tool surface + validation
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from loom.core.events import TierChanged, TierExpiryHint
from loom.core.session import LoomSession, _parse_skill_frontmatter
from loom.core.tier_manager import TierManager
from loom.core.memory.procedural import SkillGenome
from loom.platform.cli.tools import make_request_model_tier_tool, make_clear_model_tier_tool


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

class TestFrontmatterModelTier:
    def test_model_tier_extracted_when_present(self):
        raw = (
            "---\n"
            "name: deep_researcher\n"
            "description: Deep multi-step reasoning\n"
            "model_tier: 2\n"
            "---\n\n"
            "Skill body here."
        )
        name, desc, _, _, model_tier = _parse_skill_frontmatter(raw)
        assert name == "deep_researcher"
        assert desc == "Deep multi-step reasoning"
        assert model_tier == 2

    def test_model_tier_absent_returns_none(self):
        raw = (
            "---\n"
            "name: light_skill\n"
            "description: A simple skill\n"
            "---\n"
        )
        _, _, _, _, model_tier = _parse_skill_frontmatter(raw)
        assert model_tier is None

    def test_invalid_tier_value_returns_none(self):
        for bad in ("not-a-number", -1, 0, 0.5):
            raw = (
                f"---\nname: x\ndescription: y\nmodel_tier: {bad!r}\n---\n"
            )
            _, _, _, _, model_tier = _parse_skill_frontmatter(raw)
            # Parens make precedence explicit (絲絲 #277 review N1):
            #   None  → invalid value rejected
            #   int   → only allowed when input was a positive int
            assert (
                model_tier is None
                or (model_tier == int(bad) and bad > 0)
            ), bad

    def test_string_tier_coerced_to_int(self):
        raw = "---\nname: x\ndescription: y\nmodel_tier: '3'\n---\n"
        _, _, _, _, model_tier = _parse_skill_frontmatter(raw)
        assert model_tier == 3


# ---------------------------------------------------------------------------
# SkillGenome storage
# ---------------------------------------------------------------------------

class TestSkillGenomeStores:
    def test_default_model_tier_is_none(self):
        g = SkillGenome(name="x", body="...")
        assert g.model_tier is None

    def test_explicit_tier_set(self):
        g = SkillGenome(name="x", body="...", model_tier=2)
        assert g.model_tier == 2


# ---------------------------------------------------------------------------
# Session state methods (duck-typed; mirrors test_observation_masking pattern)
# ---------------------------------------------------------------------------

class _FakeSession:
    """Stand-in that mirrors LoomSession's tier surface faithfully.

    The tier state machine now lives in TierManager (loom/core/tier_manager.py);
    LoomSession's tier methods are thin delegators + read-only properties over
    ``self._tier``. We reuse the *real* delegators and property logic here (no
    re-implementation) over a real TierManager, so these tests exercise the
    actual session surface AND the real state machine — including the leaked
    private-attribute reads (``session._tier_models`` etc.) that the CLI tools
    perform. State assertions read through ``s._tier``.
    """

    _tier_models = property(lambda self: self._tier.tier_models)
    _sticky_tier = property(lambda self: self._tier.sticky_tier)
    _default_tier = property(lambda self: self._tier.default_tier)
    _turns_at_current_tier = property(lambda self: self._tier.turns_at_current_tier)
    _skill_tier_snapshot = property(lambda self: self._tier.skill_tier_snapshot)

    _active_tier = LoomSession._active_tier
    _active_model = LoomSession._active_model
    _set_sticky_tier = LoomSession._set_sticky_tier
    _tick_tier_counter = LoomSession._tick_tier_counter
    _compute_skill_max_tier = LoomSession._compute_skill_max_tier
    _refresh_runtime_identity = LoomSession._refresh_runtime_identity


def _mock_session(
    *,
    tier_models: dict[int, str] | None = None,
    default_tier: int = 1,
    sticky_tier: int | None = None,
    skill_snapshot: dict[str, int] | None = None,
    activated_skills: list[str] | None = None,
    reminder_threshold: int = 10,
    turns_at_current_tier: int = 0,
    reminder_emitted: bool = False,
    base_model: str = "minimax-m2.7",
) -> _FakeSession:
    """Build a ``_FakeSession`` backed by a real TierManager."""
    s = _FakeSession()
    s._model = base_model
    s._telemetry = None
    s._skill_outcome_tracker = SimpleNamespace(activated_skills=activated_skills or [])
    tier = TierManager(
        base_model_getter=lambda: s._model,
        tier_models=tier_models or {1: "minimax-m2.7", 2: "deepseek-v4-pro"},
        default_tier=default_tier,
        reminder_after_turns=reminder_threshold,
    )
    tier.sticky_tier = sticky_tier
    tier.turns_at_current_tier = turns_at_current_tier
    tier.reminder_emitted = reminder_emitted
    tier.skill_tier_snapshot = skill_snapshot or {}
    s._tier = tier
    return s


def _call(name: str, **kwargs: Any) -> "Any":
    """Build a ToolCall via duck typing for tool tests."""
    from loom.core.harness.middleware import ToolCall
    from loom.core.harness.permissions import TrustLevel

    return ToolCall(
        id=f"call_{name}",
        tool_name=name,
        args=kwargs,
        trust_level=TrustLevel.SAFE,
        session_id="test",
    )


class TestActiveTierAndModel:
    def test_no_sticky_returns_default(self):
        s = _mock_session(default_tier=1)
        assert s._active_tier() == 1
        assert s._active_model() == "minimax-m2.7"

    def test_sticky_overrides_default(self):
        s = _mock_session(sticky_tier=2)
        assert s._active_tier() == 2
        assert s._active_model() == "deepseek-v4-pro"

    def test_manual_model_override_wins_over_tier(self):
        s = _mock_session(sticky_tier=2, base_model="codex/gpt-5.5")
        s._tier.manual_override = True
        assert s._active_model() == "codex/gpt-5.5"

    def test_unknown_tier_falls_back_to_base_model(self):
        s = _mock_session(sticky_tier=99)
        # Tier 99 not in config → _active_model falls through to self._model
        assert s._active_model() == "minimax-m2.7"


class TestSetStickyTier:
    def test_set_default_tier_normalizes_to_none(self):
        s = _mock_session(sticky_tier=2)
        ev = s._set_sticky_tier(1, reason="done", source="agent")
        assert s._tier.sticky_tier is None  # normalized
        assert ev is not None
        assert isinstance(ev, TierChanged)
        assert ev.from_tier == 2
        assert ev.to_tier == 1
        assert ev.source == "agent"
        assert ev.reason == "done"

    def test_set_same_tier_returns_none_event(self):
        s = _mock_session(sticky_tier=2)
        ev = s._set_sticky_tier(2, reason="x", source="agent")
        assert ev is None  # no-op

    def test_escalation_emits_event(self):
        s = _mock_session(sticky_tier=None, default_tier=1)
        ev = s._set_sticky_tier(2, reason="hard", source="skill")
        assert s._tier.sticky_tier == 2
        assert ev is not None
        assert ev.from_tier == 1
        assert ev.to_tier == 2
        assert ev.from_model == "minimax-m2.7"
        assert ev.to_model == "deepseek-v4-pro"

    def test_explicit_clear_via_none(self):
        s = _mock_session(sticky_tier=2)
        ev = s._set_sticky_tier(None, reason="ok", source="clear")
        assert s._tier.sticky_tier is None
        assert ev is not None
        assert ev.to_tier == 1  # default

    def test_counter_resets_on_change(self):
        s = _mock_session(
            sticky_tier=None, turns_at_current_tier=7, reminder_emitted=True,
        )
        s._set_sticky_tier(2, reason="x", source="skill")
        assert s._tier.turns_at_current_tier == 0
        assert s._tier.reminder_emitted is False

    def test_tier_change_clears_manual_model_override(self):
        s = _mock_session(sticky_tier=None, base_model="codex/gpt-5.5")
        s._tier.manual_override = True
        s._set_sticky_tier(2, reason="x", source="user")
        assert s._tier.manual_override is False
        assert s._active_model() == "deepseek-v4-pro"


class TestComputeSkillMaxTier:
    def test_no_skills_returns_zero(self):
        s = _mock_session(activated_skills=[])
        assert s._compute_skill_max_tier() == 0

    def test_skills_without_tier_return_zero(self):
        s = _mock_session(
            activated_skills=["skill_a", "skill_b"],
            skill_snapshot={},  # neither has a tier
        )
        assert s._compute_skill_max_tier() == 0

    def test_max_across_active_skills(self):
        s = _mock_session(
            activated_skills=["light", "deep", "ultra"],
            skill_snapshot={"light": 1, "deep": 2, "ultra": 3, "irrelevant": 5},
        )
        # Only counts skills in activated_skills, not the whole snapshot
        assert s._compute_skill_max_tier() == 3

    def test_skills_active_but_unmapped_skipped(self):
        s = _mock_session(
            activated_skills=["a", "b"],
            skill_snapshot={"a": 2},  # b is unmapped
        )
        assert s._compute_skill_max_tier() == 2


class TestTickTierCounter:
    def test_default_tier_no_hint(self):
        s = _mock_session(sticky_tier=None)
        ev = s._tick_tier_counter()
        assert ev is None
        assert s._tier.turns_at_current_tier == 1  # still increments

    def test_below_threshold_no_hint(self):
        s = _mock_session(
            sticky_tier=2, turns_at_current_tier=5, reminder_threshold=10,
        )
        ev = s._tick_tier_counter()
        assert ev is None
        assert s._tier.turns_at_current_tier == 6

    def test_at_threshold_emits_hint_once(self):
        s = _mock_session(
            sticky_tier=2, turns_at_current_tier=9, reminder_threshold=10,
        )
        ev = s._tick_tier_counter()
        assert isinstance(ev, TierExpiryHint)
        assert ev.tier == 2
        assert ev.turns_used == 10
        assert ev.threshold == 10
        assert s._tier.reminder_emitted is True

        # Subsequent ticks don't re-emit
        ev2 = s._tick_tier_counter()
        assert ev2 is None

    def test_threshold_zero_disables(self):
        s = _mock_session(
            sticky_tier=2, turns_at_current_tier=99, reminder_threshold=0,
        )
        ev = s._tick_tier_counter()
        assert ev is None  # disabled


# ---------------------------------------------------------------------------
# request_model_tier tool
# ---------------------------------------------------------------------------

class TestRequestModelTierTool:
    def _session_with_queue(self, **kwargs):
        import asyncio
        s = _mock_session(**kwargs)
        s._lifecycle_events = asyncio.Queue()
        return s

    async def test_missing_reason_rejected(self):
        s = self._session_with_queue()
        tool = make_request_model_tier_tool(s)
        result = await tool.executor(_call(
            "request_model_tier", tier=2,
        ))
        assert result.success is False
        assert "reason" in result.error.lower()

    async def test_unknown_tier_rejected(self):
        s = self._session_with_queue()
        tool = make_request_model_tier_tool(s)
        result = await tool.executor(_call(
            "request_model_tier", tier=99, reason="hard",
        ))
        assert result.success is False
        assert "Tier 99 is not configured" in result.error

    async def test_escalation_succeeds_and_queues_event(self):
        s = self._session_with_queue()
        tool = make_request_model_tier_tool(s)
        result = await tool.executor(_call(
            "request_model_tier", tier=2, reason="multi-constraint puzzle",
        ))
        assert result.success
        assert s._tier.sticky_tier == 2
        # TierChanged was queued
        assert not s._lifecycle_events.empty()
        ev = s._lifecycle_events.get_nowait()
        assert isinstance(ev, TierChanged)
        assert ev.source == "agent"
        assert ev.reason == "multi-constraint puzzle"

    async def test_clear_model_tier_releases_sticky(self):
        """Issue #278: clear_model_tier should release sticky without needing a tier arg."""
        s = self._session_with_queue(sticky_tier=2)
        tool = make_clear_model_tier_tool(s)
        result = await tool.executor(_call(
            "clear_model_tier", reason="phase done",
        ))
        assert result.success
        assert s._tier.sticky_tier is None
        assert "cleared" in result.output.lower()

    async def test_clear_model_tier_rejects_empty_reason(self):
        s = self._session_with_queue(sticky_tier=2)
        tool = make_clear_model_tier_tool(s)
        result = await tool.executor(_call(
            "clear_model_tier",
        ))
        assert result.success is False
        assert "reason" in result.error.lower()

    async def test_metadata_carries_state(self):
        s = self._session_with_queue()
        tool = make_request_model_tier_tool(s)
        result = await tool.executor(_call(
            "request_model_tier", tier=2, reason="x",
        ))
        assert result.metadata["tier"] == 2
        assert result.metadata["model"] == "deepseek-v4-pro"
        assert result.metadata["sticky"] == 2
