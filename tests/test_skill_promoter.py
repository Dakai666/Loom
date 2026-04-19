"""
Tests for Issue #120 PR 3 — SkillPromoter + SkillGate + lifecycle tools + CLI.

Covers:
- ``SkillPromoter`` state transitions: shadow, maybe_auto_shadow, promote,
  rollback, deprecate — including idempotency and illegal-transition errors.
- ``SkillGate.resolve`` routing: off / auto_c / manual_b, deterministic
  slicing, per-session overrides, fallbacks when candidate is missing.
- Agent tools: ``skill_promote`` / ``skill_rollback`` invoke promoter and
  return terse summaries.
- CLI: ``_resolve_candidate_id`` short-prefix resolution; ``_skill_history``
  renders archived versions via Rich console.
"""

from __future__ import annotations

import pytest

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.core.cognition.skill_gate import (
    GateDecision,
    SHADOW_MODES,
    SkillGate,
)
from loom.core.cognition.skill_promoter import (
    PROMOTION_EVENT_KINDS,
    PromotionEvent,
    SkillPromoter,
)
from loom.core.memory.procedural import (
    ProceduralMemory,
    SkillCandidate,
    SkillGenome,
    SkillVersionRecord,
)
from loom.core.memory.store import SQLiteStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


SAMPLE_PARENT_BODY = "---\nname: s1\n---\n\n# S1\nOriginal body.\n"
SAMPLE_CANDIDATE_BODY = "---\nname: s1\n---\n\n# S1\nImproved body.\n"


@pytest.fixture
async def store(tmp_path):
    db_path = tmp_path / "memory.db"
    store = SQLiteStore(str(db_path))
    await store.initialize()
    yield store


async def _seed_parent(proc: ProceduralMemory, *, name="s1", version=1, body=SAMPLE_PARENT_BODY, confidence=0.5) -> SkillGenome:
    skill = SkillGenome(name=name, body=body, version=version, confidence=confidence, success_rate=confidence, usage_count=3)
    await proc.upsert(skill)
    return skill


async def _seed_candidate(
    proc: ProceduralMemory,
    *,
    parent_name="s1",
    parent_version=1,
    body=SAMPLE_CANDIDATE_BODY,
    status="generated",
) -> SkillCandidate:
    cand = SkillCandidate(
        parent_skill_name=parent_name,
        parent_version=parent_version,
        candidate_body=body,
        mutation_strategy="apply_suggestions",
        status=status,
    )
    await proc.insert_candidate(cand)
    return cand


# ---------------------------------------------------------------------------
# PromotionEvent
# ---------------------------------------------------------------------------


class TestPromotionEvent:
    def test_invalid_kind_raises(self):
        with pytest.raises(ValueError):
            PromotionEvent(
                kind="bogus", skill_name="s1", candidate_id=None,
                from_version=1, to_version=2,
            )

    def test_kinds_include_all_four(self):
        for k in ("auto_shadow", "promote", "rollback", "deprecate"):
            assert k in PROMOTION_EVENT_KINDS

    def test_one_line_summary_shapes(self):
        promote = PromotionEvent(
            kind="promote", skill_name="s1", candidate_id="abc",
            from_version=1, to_version=2, reason="apply",
        )
        assert "promoted" in promote.one_line_summary()
        assert "v1→v2" in promote.one_line_summary()

        rollback = PromotionEvent(
            kind="rollback", skill_name="s1", candidate_id=None,
            from_version=5, to_version=6,
        )
        assert "rollback" in rollback.one_line_summary()
        assert "v5→v6" in rollback.one_line_summary()

        shadow = PromotionEvent(
            kind="auto_shadow", skill_name="s1", candidate_id="abc",
            from_version=1, to_version=1,
        )
        assert "shadow" in shadow.one_line_summary()

        dep = PromotionEvent(
            kind="deprecate", skill_name="s1", candidate_id="abcdef",
            from_version=1, to_version=1, reason="stale",
        )
        assert "deprecated" in dep.one_line_summary()
        assert "abcdef" in dep.one_line_summary()


# ---------------------------------------------------------------------------
# SkillPromoter — shadow / maybe_auto_shadow
# ---------------------------------------------------------------------------


class TestShadow:
    @pytest.mark.asyncio
    async def test_shadow_moves_generated_to_shadow(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_parent(proc)
            cand = await _seed_candidate(proc)
            promoter = SkillPromoter(procedural=proc)
            events: list[PromotionEvent] = []
            promoter.subscribe(lambda e: _collect(events, e))

            updated = await promoter.shadow(cand.id, reason="trial")

            assert updated is not None
            assert updated.status == "shadow"
            assert "shadow: trial" in (updated.notes or "")
            assert len(events) == 1
            assert events[0].kind == "auto_shadow"
            assert events[0].reason == "trial"

    @pytest.mark.asyncio
    async def test_shadow_is_idempotent_when_already_shadow(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_parent(proc)
            cand = await _seed_candidate(proc, status="shadow")
            promoter = SkillPromoter(procedural=proc)
            events: list[PromotionEvent] = []
            promoter.subscribe(lambda e: _collect(events, e))

            result = await promoter.shadow(cand.id)

            assert result is not None
            assert result.status == "shadow"
            assert events == []  # no transition → no event

    @pytest.mark.asyncio
    async def test_shadow_rejects_terminal_state(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_parent(proc)
            cand = await _seed_candidate(proc, status="promoted")
            promoter = SkillPromoter(procedural=proc)

            with pytest.raises(ValueError):
                await promoter.shadow(cand.id)

    @pytest.mark.asyncio
    async def test_shadow_returns_none_for_missing_candidate(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            promoter = SkillPromoter(procedural=proc)
            assert await promoter.shadow("no-such-id") is None

    @pytest.mark.asyncio
    async def test_maybe_auto_shadow_fires_when_under_ceiling(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_parent(proc, confidence=0.6)  # ≤ ceiling 0.7
            cand = await _seed_candidate(proc)
            promoter = SkillPromoter(
                procedural=proc, shadow_mode="auto_c",
                auto_shadow_confidence_ceiling=0.7,
            )

            result = await promoter.maybe_auto_shadow(cand.id)
            assert result is not None
            assert result.status == "shadow"

    @pytest.mark.asyncio
    async def test_maybe_auto_shadow_skips_when_parent_above_ceiling(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_parent(proc, confidence=0.95)  # above 0.7
            cand = await _seed_candidate(proc)
            promoter = SkillPromoter(procedural=proc, shadow_mode="auto_c")

            assert await promoter.maybe_auto_shadow(cand.id) is None
            latest = await proc.get_candidate(cand.id)
            assert latest.status == "generated"

    @pytest.mark.asyncio
    async def test_maybe_auto_shadow_disabled_in_manual_b(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_parent(proc, confidence=0.1)
            cand = await _seed_candidate(proc)
            promoter = SkillPromoter(procedural=proc, shadow_mode="manual_b")

            assert await promoter.maybe_auto_shadow(cand.id) is None


# ---------------------------------------------------------------------------
# SkillPromoter — promote
# ---------------------------------------------------------------------------


class TestPromote:
    @pytest.mark.asyncio
    async def test_promote_swaps_body_and_bumps_version(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_parent(proc, confidence=0.4)
            cand = await _seed_candidate(proc)
            promoter = SkillPromoter(procedural=proc)
            events: list[PromotionEvent] = []
            promoter.subscribe(lambda e: _collect(events, e))

            new_parent = await promoter.promote(cand.id, reason="apply")

            assert new_parent is not None
            assert new_parent.version == 2
            assert new_parent.body == SAMPLE_CANDIDATE_BODY
            # Confidence reset to track the new body.
            assert new_parent.confidence == 1.0
            assert new_parent.success_rate == 1.0
            assert new_parent.usage_count == 0

            updated_cand = await proc.get_candidate(cand.id)
            assert updated_cand.status == "promoted"

            # Old body archived.
            history = await proc.list_history("s1")
            assert len(history) == 1
            assert history[0].body == SAMPLE_PARENT_BODY
            assert history[0].reason == "promote"
            assert history[0].source_candidate_id == cand.id

            assert len(events) == 1
            assert events[0].kind == "promote"
            assert events[0].from_version == 1
            assert events[0].to_version == 2

    @pytest.mark.asyncio
    async def test_promote_deprecates_sibling_shadows(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_parent(proc)
            winner = await _seed_candidate(proc, status="shadow")
            sibling = await _seed_candidate(proc, status="shadow", body="---\nname: s1\n---\n\n# S1\nOther trial.\n")
            promoter = SkillPromoter(procedural=proc)

            await promoter.promote(winner.id)

            refreshed_sib = await proc.get_candidate(sibling.id)
            assert refreshed_sib.status == "deprecated"
            assert winner.id in (refreshed_sib.notes or "")

    @pytest.mark.asyncio
    async def test_promote_is_idempotent(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_parent(proc)
            cand = await _seed_candidate(proc, status="promoted")
            promoter = SkillPromoter(procedural=proc)

            parent = await promoter.promote(cand.id)
            assert parent is not None
            assert parent.version == 1  # nothing happened

    @pytest.mark.asyncio
    async def test_promote_rejects_terminal_state(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_parent(proc)
            cand = await _seed_candidate(proc, status="deprecated")
            promoter = SkillPromoter(procedural=proc)

            with pytest.raises(ValueError):
                await promoter.promote(cand.id)

    @pytest.mark.asyncio
    async def test_promote_returns_none_for_missing_parent(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            cand = await _seed_candidate(proc)  # no parent seeded
            promoter = SkillPromoter(procedural=proc)

            assert await promoter.promote(cand.id) is None


# ---------------------------------------------------------------------------
# SkillPromoter — rollback
# ---------------------------------------------------------------------------


class TestRollback:
    @pytest.mark.asyncio
    async def test_rollback_to_latest_archived(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_parent(proc)
            cand = await _seed_candidate(proc)
            promoter = SkillPromoter(procedural=proc)
            events: list[PromotionEvent] = []

            await promoter.promote(cand.id, reason="apply")
            promoter.subscribe(lambda e: _collect(events, e))
            result = await promoter.rollback("s1", reason="regression")

            assert result is not None
            assert result.body == SAMPLE_PARENT_BODY
            assert result.version == 3  # promote bumped to 2, rollback to 3

            refreshed_cand = await proc.get_candidate(cand.id)
            assert refreshed_cand.status == "rolled_back"

            assert len(events) == 1
            assert events[0].kind == "rollback"
            assert events[0].reason == "regression"

    @pytest.mark.asyncio
    async def test_rollback_to_specific_version(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_parent(proc)
            c1 = await _seed_candidate(proc)
            promoter = SkillPromoter(procedural=proc)

            await promoter.promote(c1.id)
            # Second promote → another archive entry at v2.
            c2 = await _seed_candidate(proc, parent_version=2, body="---\nname: s1\n---\n\n# S1\nThird.\n")
            await promoter.promote(c2.id)

            restored = await promoter.rollback("s1", to_version=1)
            assert restored is not None
            assert restored.body == SAMPLE_PARENT_BODY  # v1 restored

    @pytest.mark.asyncio
    async def test_rollback_returns_none_when_no_history(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_parent(proc)
            promoter = SkillPromoter(procedural=proc)

            assert await promoter.rollback("s1") is None

    @pytest.mark.asyncio
    async def test_rollback_returns_none_for_missing_skill(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            promoter = SkillPromoter(procedural=proc)
            assert await promoter.rollback("no-such-skill") is None


# ---------------------------------------------------------------------------
# SkillPromoter — deprecate
# ---------------------------------------------------------------------------


class TestDeprecate:
    @pytest.mark.asyncio
    async def test_deprecate_generated_candidate(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_parent(proc)
            cand = await _seed_candidate(proc)
            promoter = SkillPromoter(procedural=proc)
            events: list[PromotionEvent] = []
            promoter.subscribe(lambda e: _collect(events, e))

            result = await promoter.deprecate(cand.id, reason="bad idea")
            assert result is not None
            assert result.status == "deprecated"
            assert events and events[0].kind == "deprecate"

    @pytest.mark.asyncio
    async def test_deprecate_idempotent(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_parent(proc)
            cand = await _seed_candidate(proc, status="deprecated")
            promoter = SkillPromoter(procedural=proc)

            result = await promoter.deprecate(cand.id)
            assert result.status == "deprecated"

    @pytest.mark.asyncio
    async def test_deprecate_rejects_promoted(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_parent(proc)
            cand = await _seed_candidate(proc, status="promoted")
            promoter = SkillPromoter(procedural=proc)
            with pytest.raises(ValueError):
                await promoter.deprecate(cand.id)


# ---------------------------------------------------------------------------
# SkillGate
# ---------------------------------------------------------------------------


class TestSkillGate:
    @pytest.mark.asyncio
    async def test_off_mode_always_returns_parent(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            skill = await _seed_parent(proc)
            await _seed_candidate(proc, status="shadow")

            gate = SkillGate(procedural=proc, shadow_mode="off", session_id="sess-1")
            decision = await gate.resolve(skill)
            assert decision.source == "parent"
            assert decision.is_shadow is False
            assert decision.audit_tag() == "parent"

    @pytest.mark.asyncio
    async def test_manual_b_never_auto_serves_shadow(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            skill = await _seed_parent(proc)
            await _seed_candidate(proc, status="shadow")

            gate = SkillGate(
                procedural=proc, shadow_mode="manual_b",
                shadow_fraction=1.0, session_id="sess-1",
            )
            decision = await gate.resolve(skill)
            assert decision.source == "parent"

    @pytest.mark.asyncio
    async def test_auto_c_serves_shadow_when_fraction_is_one(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            skill = await _seed_parent(proc)
            cand = await _seed_candidate(proc, status="shadow")

            gate = SkillGate(
                procedural=proc, shadow_mode="auto_c",
                shadow_fraction=1.0, session_id="sess-1",
            )
            decision = await gate.resolve(skill)
            assert decision.source == "shadow"
            assert decision.candidate_id == cand.id
            assert decision.body == SAMPLE_CANDIDATE_BODY
            assert decision.audit_tag().startswith("shadow:")

    @pytest.mark.asyncio
    async def test_auto_c_serves_parent_when_fraction_is_zero(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            skill = await _seed_parent(proc)
            await _seed_candidate(proc, status="shadow")

            gate = SkillGate(
                procedural=proc, shadow_mode="auto_c",
                shadow_fraction=0.0, session_id="sess-1",
            )
            decision = await gate.resolve(skill)
            assert decision.source == "parent"

    @pytest.mark.asyncio
    async def test_auto_c_slicing_is_deterministic_per_session(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            skill = await _seed_parent(proc)
            await _seed_candidate(proc, status="shadow")

            gate = SkillGate(
                procedural=proc, shadow_mode="auto_c",
                shadow_fraction=0.5, session_id="stable-session",
            )
            d1 = await gate.resolve(skill)
            d2 = await gate.resolve(skill)
            assert d1.source == d2.source  # same side twice

    @pytest.mark.asyncio
    async def test_force_shadow_overrides_slicing(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            skill = await _seed_parent(proc)
            cand = await _seed_candidate(proc, status="shadow")

            gate = SkillGate(
                procedural=proc, shadow_mode="manual_b",
                shadow_fraction=0.0, session_id="s",
            )
            gate.force_shadow("s1", cand.id)

            decision = await gate.resolve(skill)
            assert decision.source == "shadow"
            assert decision.candidate_id == cand.id

    @pytest.mark.asyncio
    async def test_force_parent_overrides(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            skill = await _seed_parent(proc)
            await _seed_candidate(proc, status="shadow")

            gate = SkillGate(
                procedural=proc, shadow_mode="auto_c",
                shadow_fraction=1.0, session_id="s",
            )
            gate.force_parent("s1")
            decision = await gate.resolve(skill)
            assert decision.source == "parent"

    @pytest.mark.asyncio
    async def test_clear_override_returns_to_slicing(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            skill = await _seed_parent(proc)
            await _seed_candidate(proc, status="shadow")

            gate = SkillGate(
                procedural=proc, shadow_mode="auto_c",
                shadow_fraction=1.0, session_id="s",
            )
            gate.force_parent("s1")
            gate.clear_override("s1")
            decision = await gate.resolve(skill)
            assert decision.source == "shadow"

    @pytest.mark.asyncio
    async def test_falls_back_to_parent_when_no_shadow_exists(self, store):
        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            skill = await _seed_parent(proc)
            # Candidate exists but only in 'generated' state.
            await _seed_candidate(proc, status="generated")

            gate = SkillGate(
                procedural=proc, shadow_mode="auto_c",
                shadow_fraction=1.0, session_id="s",
            )
            decision = await gate.resolve(skill)
            assert decision.source == "parent"

    def test_unknown_shadow_mode_falls_back_to_off(self, store):
        # Store fixture provided just for symmetry; gate doesn't use it here.
        gate = SkillGate(procedural=object(), shadow_mode="nonsense")
        assert gate.shadow_mode == "off"

    def test_shadow_fraction_is_clamped(self, store):
        gate = SkillGate(procedural=object(), shadow_fraction=5.0)
        assert gate.shadow_fraction == 1.0
        gate = SkillGate(procedural=object(), shadow_fraction=-1.0)
        assert gate.shadow_fraction == 0.0

    def test_shadow_modes_tuple(self):
        assert SHADOW_MODES == ("off", "auto_c", "manual_b")

    def test_gate_decision_audit_tag_truncates(self):
        d = GateDecision(
            body="b", source="shadow", served_version=1,
            candidate_id="0123456789abcdef",
        )
        assert d.audit_tag() == "shadow:01234567"


# ---------------------------------------------------------------------------
# Agent tools — skill_promote / skill_rollback
# ---------------------------------------------------------------------------


def _make_tool_call(tool_name: str, args: dict) -> ToolCall:
    return ToolCall(
        tool_name=tool_name, args=args,
        trust_level=TrustLevel.GUARDED, session_id="sess-test",
    )


class TestSkillTools:
    @pytest.mark.asyncio
    async def test_promote_tool_calls_promoter(self, store):
        from loom.platform.cli.tools import make_skill_promote_tool

        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_parent(proc)
            cand = await _seed_candidate(proc)
            promoter = SkillPromoter(procedural=proc)

            tool = make_skill_promote_tool(promoter)
            assert tool.trust_level == TrustLevel.GUARDED
            call = _make_tool_call("promote_skill_candidate", {
                "candidate_id": cand.id, "reason": "test",
            })
            result = await tool.executor(call)
            assert result.success is True
            assert "v2" in (result.output or "")
            assert result.metadata["new_version"] == 2

    @pytest.mark.asyncio
    async def test_promote_tool_handles_missing_candidate(self, store):
        from loom.platform.cli.tools import make_skill_promote_tool

        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            promoter = SkillPromoter(procedural=proc)
            tool = make_skill_promote_tool(promoter)

            result = await tool.executor(
                _make_tool_call("promote_skill_candidate", {"candidate_id": "no-such-id"})
            )
            assert result.success is False
            assert "not found" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_promote_tool_requires_candidate_id(self, store):
        from loom.platform.cli.tools import make_skill_promote_tool

        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            tool = make_skill_promote_tool(SkillPromoter(procedural=proc))
            result = await tool.executor(
                _make_tool_call("promote_skill_candidate", {})
            )
            assert result.success is False
            assert "candidate_id" in (result.error or "")

    @pytest.mark.asyncio
    async def test_rollback_tool_calls_promoter(self, store):
        from loom.platform.cli.tools import make_skill_rollback_tool

        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_parent(proc)
            cand = await _seed_candidate(proc)
            promoter = SkillPromoter(procedural=proc)
            await promoter.promote(cand.id)

            tool = make_skill_rollback_tool(promoter)
            assert tool.trust_level == TrustLevel.GUARDED
            result = await tool.executor(
                _make_tool_call("rollback_skill", {"skill_name": "s1", "reason": "revert"})
            )
            assert result.success is True
            assert "v3" in (result.output or "")

    @pytest.mark.asyncio
    async def test_rollback_tool_handles_bad_version(self, store):
        from loom.platform.cli.tools import make_skill_rollback_tool

        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            tool = make_skill_rollback_tool(SkillPromoter(procedural=proc))
            result = await tool.executor(
                _make_tool_call("rollback_skill", {"skill_name": "s1", "to_version": "abc"})
            )
            assert result.success is False
            assert "integer" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_rollback_tool_requires_skill_name(self, store):
        from loom.platform.cli.tools import make_skill_rollback_tool

        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            tool = make_skill_rollback_tool(SkillPromoter(procedural=proc))
            result = await tool.executor(
                _make_tool_call("rollback_skill", {})
            )
            assert result.success is False
            assert "skill_name" in (result.error or "")


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


class TestCLIHelpers:
    @pytest.mark.asyncio
    async def test_resolve_candidate_id_short_prefix_unique(self, store):
        from loom.platform.cli.main import _resolve_candidate_id

        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            cand = await _seed_candidate(proc)
            prefix = cand.id[:8]

            resolved = await _resolve_candidate_id(proc, prefix)
            assert resolved == cand.id

    @pytest.mark.asyncio
    async def test_resolve_candidate_id_full_id_passthrough(self, store):
        from loom.platform.cli.main import _resolve_candidate_id

        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            # Full-length uuid (≥32 chars) passes through without lookup.
            full = "a" * 36
            resolved = await _resolve_candidate_id(proc, full)
            assert resolved == full

    @pytest.mark.asyncio
    async def test_resolve_candidate_id_rejects_short_prefix(self, store):
        from loom.platform.cli.main import _resolve_candidate_id

        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            assert await _resolve_candidate_id(proc, "ab") is None

    @pytest.mark.asyncio
    async def test_resolve_candidate_id_returns_none_on_no_match(self, store):
        from loom.platform.cli.main import _resolve_candidate_id

        async with store.connect() as conn:
            proc = ProceduralMemory(conn)
            await _seed_candidate(proc)
            assert await _resolve_candidate_id(proc, "deadbeef") is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect(bucket: list, event):
    """Sync helper used as an async subscriber.

    SkillPromoter awaits whatever the subscriber returns; returning the
    coroutine from ``_append`` keeps the contract while letting tests use a
    plain list.
    """
    async def _append():
        bucket.append(event)
    return _append()
