"""
Tests for Issue #120 PR 1 — TaskReflector + TaskDiagnostic.

Covers:
- TaskDiagnostic dataclass: JSON round-trip, one-line summary, timestamp fidelity.
- ``_parse_diagnostic`` resilience: markdown-fenced JSON, extra prose, closed
  ``task_type`` label set, quality-score clamping, garbage input.
- End-to-end ``_reflect_one``: SemanticMemory write, SkillGenome confidence
  EMA update, envelope_ids populated, subscriber notification.
- Visibility / enabled toggles: "off" disables everything; "summary" /
  "verbose" both persist and notify.
"""

from __future__ import annotations

import json
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock

import pytest

from loom.core.cognition.task_reflector import (
    TASK_TYPES,
    TaskDiagnostic,
    TaskReflector,
    _parse_diagnostic,
)
from loom.core.events import ExecutionEnvelopeView, ExecutionNodeView
from loom.core.memory.procedural import SkillGenome
from loom.core.memory.semantic import SemanticEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_diagnostic(**overrides) -> TaskDiagnostic:
    defaults = dict(
        skill_name="test-skill",
        session_id="s1",
        turn_index=3,
        task_type="code_review",
        task_type_confidence=0.8,
        instructions_followed=["Followed step 1"],
        instructions_violated=["Skipped step 3"],
        failure_patterns=["Rushed final check"],
        success_patterns=["Used grep before editing"],
        mutation_suggestions=["Add explicit 'verify with tests' after step 3"],
        quality_score=3.5,
        envelope_ids=["e1", "e2"],
    )
    defaults.update(overrides)
    return TaskDiagnostic(**defaults)


def _make_node(tool_name="run_bash", state="completed") -> ExecutionNodeView:
    return ExecutionNodeView(
        node_id="n1",
        call_id="c1",
        action_id="n1",
        tool_name=tool_name,
        level=0,
        state=state,
        trust_level="SAFE",
        args_preview="ls",
        duration_ms=12.3,
    )


def _make_envelope(envelope_id="e1") -> ExecutionEnvelopeView:
    return ExecutionEnvelopeView(
        envelope_id=envelope_id,
        session_id="s1",
        turn_index=3,
        status="completed",
        node_count=1,
        parallel_groups=1,
        elapsed_ms=45.0,
        levels=[["n1"]],
        nodes=[_make_node()],
    )


def _valid_llm_payload() -> str:
    return json.dumps(
        {
            "task_type": "code_review",
            "task_type_confidence": 0.9,
            "instructions_followed": ["Read the file first"],
            "instructions_violated": ["Did not run tests"],
            "failure_patterns": ["Skipped verification"],
            "success_patterns": ["Used minimal edits"],
            "mutation_suggestions": ["Insert 'run pytest' after every edit"],
            "quality_score": 3.5,
        }
    )


# ---------------------------------------------------------------------------
# TaskDiagnostic dataclass
# ---------------------------------------------------------------------------


class TestTaskDiagnostic:
    def test_json_round_trip(self):
        d = _make_diagnostic()
        rehydrated = TaskDiagnostic.from_json(d.to_json())
        assert rehydrated.skill_name == d.skill_name
        assert rehydrated.task_type == d.task_type
        assert rehydrated.quality_score == pytest.approx(d.quality_score)
        assert rehydrated.envelope_ids == d.envelope_ids
        # Timestamp survives ISO round-trip at second precision.
        assert rehydrated.timestamp.isoformat() == d.timestamp.isoformat()

    def test_one_line_summary_includes_suggestion(self):
        d = _make_diagnostic(mutation_suggestions=["Add explicit 'verify' step"])
        s = d.one_line_summary()
        assert "test-skill" in s
        assert "3.5" in s
        assert "verify" in s

    def test_one_line_summary_without_suggestion(self):
        d = _make_diagnostic(mutation_suggestions=[])
        s = d.one_line_summary()
        assert "test-skill" in s
        assert "·" in s  # separator present

    def test_one_line_summary_truncates_long_suggestion(self):
        long = "x" * 200
        d = _make_diagnostic(mutation_suggestions=[long])
        s = d.one_line_summary()
        assert "…" in s
        assert len(s) < 160


# ---------------------------------------------------------------------------
# _parse_diagnostic defensive parsing
# ---------------------------------------------------------------------------


class TestParseDiagnostic:
    def test_valid_payload(self):
        parsed = _parse_diagnostic(_valid_llm_payload())
        assert parsed is not None
        assert parsed["task_type"] == "code_review"
        assert parsed["quality_score"] == pytest.approx(3.5)
        assert parsed["instructions_followed"] == ["Read the file first"]

    def test_markdown_fenced(self):
        wrapped = f"```json\n{_valid_llm_payload()}\n```"
        parsed = _parse_diagnostic(wrapped)
        assert parsed is not None
        assert parsed["task_type"] == "code_review"

    def test_embedded_json_with_preamble(self):
        raw = (
            "Here's my analysis:\n\n"
            + _valid_llm_payload()
            + "\n\nHope that helps."
        )
        parsed = _parse_diagnostic(raw)
        assert parsed is not None
        assert parsed["task_type"] == "code_review"

    def test_unknown_task_type_falls_back_to_other(self):
        bad = json.loads(_valid_llm_payload())
        bad["task_type"] = "wildcard-type"
        parsed = _parse_diagnostic(json.dumps(bad))
        assert parsed is not None
        assert parsed["task_type"] == "other"
        assert "other" in TASK_TYPES

    def test_quality_score_clamped(self):
        bad = json.loads(_valid_llm_payload())
        bad["quality_score"] = 99
        parsed = _parse_diagnostic(json.dumps(bad))
        assert parsed is not None
        assert parsed["quality_score"] == 5.0

        bad["quality_score"] = -3
        parsed = _parse_diagnostic(json.dumps(bad))
        assert parsed is not None
        assert parsed["quality_score"] == 1.0

    def test_confidence_clamped(self):
        bad = json.loads(_valid_llm_payload())
        bad["task_type_confidence"] = 7.5
        parsed = _parse_diagnostic(json.dumps(bad))
        assert parsed is not None
        assert parsed["task_type_confidence"] == 1.0

    def test_non_list_fields_coerced_to_empty_list(self):
        bad = json.loads(_valid_llm_payload())
        bad["mutation_suggestions"] = "not a list"
        parsed = _parse_diagnostic(json.dumps(bad))
        assert parsed is not None
        assert parsed["mutation_suggestions"] == []

    def test_list_item_truncation(self):
        bad = json.loads(_valid_llm_payload())
        bad["mutation_suggestions"] = ["x" * 500]
        parsed = _parse_diagnostic(json.dumps(bad))
        assert parsed is not None
        assert len(parsed["mutation_suggestions"][0]) == 200

    def test_garbage_returns_none(self):
        assert _parse_diagnostic("I did a great job!") is None

    def test_non_dict_json_returns_none(self):
        assert _parse_diagnostic("[1, 2, 3]") is None


# ---------------------------------------------------------------------------
# TaskReflector._reflect_one end-to-end
# ---------------------------------------------------------------------------


def _build_reflector(
    *,
    router_response_text: str | None = None,
    skill: SkillGenome | None = None,
    enabled: bool = True,
    visibility: str = "summary",
) -> tuple[TaskReflector, dict]:
    """Return a reflector wired to fully-mocked deps plus the mocks."""

    if router_response_text is None:
        router_response_text = _valid_llm_payload()
    if skill is None:
        skill = SkillGenome(name="test-skill", body="# Test\nDo X then Y.")

    router = MagicMock()
    router.chat = AsyncMock(
        return_value=MagicMock(text=router_response_text)
    )

    procedural = AsyncMock()
    procedural.get = AsyncMock(return_value=skill)
    procedural.upsert = AsyncMock()

    semantic = AsyncMock()
    semantic.upsert = AsyncMock()

    reflector = TaskReflector(
        router=router,
        model="test-model",
        memory=MagicMock(
            procedural=procedural,
            semantic=semantic,
            relational=None,
            episodic=None,
        ),
        session_id="session-xyz",
        enabled=enabled,
        visibility=visibility,
    )

    return reflector, {
        "router": router,
        "procedural": procedural,
        "semantic": semantic,
        "skill": skill,
    }


class TestReflectOneEndToEnd:
    @pytest.mark.asyncio
    async def test_persists_diagnostic_to_semantic(self):
        reflector, mocks = _build_reflector()
        envelope = _make_envelope()

        diag = await reflector._reflect_one(
            skill_name="test-skill",
            turn_index=3,
            turn_summary="Completed the code review.",
            envelopes=[envelope],
            tool_count=2,
        )

        assert diag is not None
        assert diag.skill_name == "test-skill"
        assert diag.envelope_ids == [envelope.envelope_id]
        assert diag.task_type in TASK_TYPES

        # SemanticMemory.upsert called once with a diagnostic-keyed entry.
        mocks["semantic"].upsert.assert_awaited_once()
        entry: SemanticEntry = mocks["semantic"].upsert.await_args.args[0]
        assert entry.key.startswith("skill:test-skill:diagnostic:")
        assert entry.source == "task_reflector:session-xyz"
        assert entry.metadata["quality_score"] == pytest.approx(3.5)
        assert entry.metadata["tool_count"] == 2

        # The JSON body round-trips back to an equivalent diagnostic.
        rehydrated = TaskDiagnostic.from_json(entry.value)
        assert rehydrated.skill_name == diag.skill_name
        assert rehydrated.quality_score == pytest.approx(diag.quality_score)

    @pytest.mark.asyncio
    async def test_confidence_ema_applied(self):
        skill = SkillGenome(
            name="test-skill",
            body="# Skill",
            confidence=0.5,
            success_rate=0.5,
            usage_count=0,
        )
        reflector, mocks = _build_reflector(skill=skill)

        await reflector._reflect_one(
            skill_name="test-skill",
            turn_index=1,
            turn_summary="done",
            envelopes=[_make_envelope()],
            tool_count=0,
        )

        # quality_score=3.5 → normalised 0.7; EMA with alpha=0.15 over 0.5:
        #   0.85*0.5 + 0.15*0.7 = 0.425 + 0.105 = 0.53
        mocks["procedural"].upsert.assert_awaited_once()
        updated: SkillGenome = mocks["procedural"].upsert.await_args.args[0]
        assert updated.confidence == pytest.approx(0.53, abs=1e-3)
        assert updated.success_rate == pytest.approx(0.53, abs=1e-3)
        assert updated.usage_count == 1

    @pytest.mark.asyncio
    async def test_missing_skill_returns_none(self):
        reflector, mocks = _build_reflector()
        mocks["procedural"].get = AsyncMock(return_value=None)

        result = await reflector._reflect_one(
            skill_name="ghost-skill",
            turn_index=1,
            turn_summary="x",
            envelopes=[],
            tool_count=0,
        )
        assert result is None
        mocks["semantic"].upsert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unparseable_llm_output_returns_none(self):
        reflector, mocks = _build_reflector(router_response_text="gibberish no json")

        result = await reflector._reflect_one(
            skill_name="test-skill",
            turn_index=1,
            turn_summary="x",
            envelopes=[],
            tool_count=0,
        )
        assert result is None
        mocks["semantic"].upsert.assert_not_awaited()
        mocks["procedural"].upsert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_subscriber_receives_diagnostic(self):
        reflector, _ = _build_reflector()

        received: list[TaskDiagnostic] = []

        async def _cb(diag: TaskDiagnostic) -> None:
            received.append(diag)

        reflector.subscribe(_cb)
        await reflector._reflect_one(
            skill_name="test-skill",
            turn_index=1,
            turn_summary="done",
            envelopes=[_make_envelope()],
            tool_count=0,
        )

        assert len(received) == 1
        assert received[0].skill_name == "test-skill"


# ---------------------------------------------------------------------------
# Toggles
# ---------------------------------------------------------------------------


class TestReflectorToggles:
    def test_enabled_respects_visibility_off(self):
        reflector, _ = _build_reflector(visibility="off")
        assert reflector.enabled is False

    def test_enabled_true_for_summary_and_verbose(self):
        for vis in ("summary", "verbose"):
            reflector, _ = _build_reflector(visibility=vis)
            assert reflector.enabled is True
            assert reflector.visibility == vis

    def test_invalid_visibility_falls_back_to_summary(self):
        reflector, _ = _build_reflector(visibility="loud")
        assert reflector.visibility == "summary"

    @pytest.mark.asyncio
    async def test_off_still_drains_tracker(self):
        """With visibility=off, maybe_reflect must still clear activations so
        the next turn isn't double-counted."""
        reflector, mocks = _build_reflector(visibility="off")

        tracker = MagicMock()
        tracker.drain_for_reflection = MagicMock(return_value=[])

        reflector.maybe_reflect(
            tracker=tracker,
            turn_index=5,
            turn_summary="done",
            envelopes=[],
        )

        tracker.drain_for_reflection.assert_called_once_with(5)
        mocks["router"].chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_subscribers_skipped_when_off(self):
        reflector, _ = _build_reflector(visibility="off")
        received: list[TaskDiagnostic] = []

        async def _cb(diag: TaskDiagnostic) -> None:
            received.append(diag)

        reflector.subscribe(_cb)
        await reflector._notify_subscribers(_make_diagnostic())
        assert received == []
