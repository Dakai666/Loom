"""Tests for BatchDiagnostic and SkillMutator.from_batch_diagnostic (Issue #120 PR 4)."""
from __future__ import annotations

from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from loom.core.cognition.task_reflector import BatchDiagnostic, TaskDiagnostic
from loom.core.memory.procedural import SkillCandidate, SkillGenome
from loom.core.memory.store import SQLiteStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_diagnostic(
    skill_name: str = "test-skill",
    quality_score: float = 2.5,
    suggestions: list[str] | None = None,
) -> TaskDiagnostic:
    return TaskDiagnostic(
        skill_name=skill_name,
        session_id="sess-1",
        turn_index=1,
        task_type="general",
        task_type_confidence=0.9,
        instructions_followed=["step A"],
        instructions_violated=[],
        failure_patterns=["skipped step B"],
        success_patterns=[],
        mutation_suggestions=["add step B after step A"] if suggestions is None else suggestions,
        quality_score=quality_score,
    )


_GENOME_BODY = (
    "---\nname: test-skill\n---\n\n# Test Skill\n\n"
    "## Core Principles\n\n1. Always verify inputs before proceeding.\n"
    "2. Log every significant decision for audit purposes.\n\n"
    "## Workflow\n\n### Step One: Validate inputs\nCheck all required fields.\n"
)
_LLM_BODY = (
    "---\nname: test-skill\n---\n\n# Test Skill\n\n"
    "## Core Principles\n\n1. Always verify inputs before proceeding.\n"
    "2. Log every significant decision for audit purposes.\n\n"
    "## Workflow\n\n### Step One: Validate inputs\nCheck all required fields.\n"
    "### Step Two: New step added by mutator\nDo the thing properly.\n"
)


def _make_genome(body: str | None = None) -> SkillGenome:
    return SkillGenome(name="test-skill", body=body if body is not None else _GENOME_BODY)


# ---------------------------------------------------------------------------
# BatchDiagnostic properties
# ---------------------------------------------------------------------------

class TestBatchDiagnostic:
    def test_avg_quality_score(self):
        batch = BatchDiagnostic(
            skill_name="s",
            diagnostics=[
                _make_diagnostic(quality_score=2.0),
                _make_diagnostic(quality_score=4.0),
            ],
            pass_rate=0.5,
        )
        assert batch.avg_quality_score == pytest.approx(3.0)

    def test_avg_quality_score_empty(self):
        batch = BatchDiagnostic(skill_name="s", diagnostics=[], pass_rate=0.0)
        assert batch.avg_quality_score == 0.0

    def test_improvement_none_when_no_previous(self):
        batch = BatchDiagnostic(skill_name="s", diagnostics=[], pass_rate=0.8)
        assert batch.improvement is None

    def test_improvement_calculated(self):
        batch = BatchDiagnostic(
            skill_name="s", diagnostics=[], pass_rate=0.9, previous_pass_rate=0.65
        )
        assert batch.improvement == pytest.approx(0.25)

    def test_aggregated_suggestions_deduplicated(self):
        d1 = _make_diagnostic(suggestions=["fix A", "fix B"])
        d2 = _make_diagnostic(suggestions=["fix B", "fix C"])
        batch = BatchDiagnostic(skill_name="s", diagnostics=[d1, d2], pass_rate=0.5)
        suggestions = batch.aggregated_suggestions
        assert suggestions == ["fix A", "fix B", "fix C"]

    def test_one_line_summary_with_improvement(self):
        batch = BatchDiagnostic(
            skill_name="my-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.9,
            previous_pass_rate=0.6,
        )
        summary = batch.one_line_summary()
        assert "my-skill" in summary
        assert "90%" in summary
        assert "+30%" in summary

    def test_one_line_summary_no_improvement(self):
        batch = BatchDiagnostic(
            skill_name="my-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.7,
        )
        summary = batch.one_line_summary()
        assert "+" not in summary


# ---------------------------------------------------------------------------
# SkillMutator.from_batch_diagnostic
# ---------------------------------------------------------------------------

class TestFromBatchDiagnostic:
    def _make_mutator(self, enabled: bool = True, llm_body: str | None = None):
        from loom.core.cognition.skill_mutator import SkillMutator

        router = MagicMock()
        mock_response = MagicMock()
        mock_response.text = llm_body or _LLM_BODY
        router.chat = AsyncMock(return_value=mock_response)

        return SkillMutator(router=router, model="test-model", enabled=enabled)

    async def test_returns_none_when_disabled(self):
        mutator = self._make_mutator(enabled=False)
        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.5,
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is None

    async def test_returns_none_when_no_suggestions(self):
        mutator = self._make_mutator()
        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic(suggestions=[])],
            pass_rate=0.5,
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is None

    async def test_returns_none_when_empty_parent_body(self):
        mutator = self._make_mutator()
        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.5,
        )
        result = await mutator.from_batch_diagnostic(_make_genome(body="   "), batch)
        assert result is None

    async def test_fast_track_set_when_improvement_above_threshold(self):
        mutator = self._make_mutator()
        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.9,
            previous_pass_rate=0.65,  # improvement = 0.25 ≥ 0.20
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is not None
        assert result.candidate.fast_track is True

    async def test_fast_track_false_when_improvement_below_threshold(self):
        mutator = self._make_mutator()
        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.75,
            previous_pass_rate=0.65,  # improvement = 0.10 < 0.20
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is not None
        assert result.candidate.fast_track is False

    async def test_fast_track_false_when_no_previous(self):
        mutator = self._make_mutator()
        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.9,
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is not None
        assert result.candidate.fast_track is False

    async def test_mutation_strategy_is_batch(self):
        mutator = self._make_mutator()
        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.5,
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is not None
        assert result.candidate.mutation_strategy == "batch_meta_skill_engineer"

    async def test_pass_rate_in_pareto_scores(self):
        mutator = self._make_mutator()
        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.75,
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is not None
        assert result.candidate.pareto_scores.get("pass_rate") == pytest.approx(0.75)

    async def test_llm_error_returns_none(self):
        from loom.core.cognition.skill_mutator import SkillMutator

        router = MagicMock()
        router.chat = AsyncMock(side_effect=RuntimeError("network down"))
        mutator = SkillMutator(router=router, model="test-model", enabled=True)

        batch = BatchDiagnostic(
            skill_name="test-skill",
            diagnostics=[_make_diagnostic()],
            pass_rate=0.5,
        )
        result = await mutator.from_batch_diagnostic(_make_genome(), batch)
        assert result is None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_conn(tmp_path):
    store = SQLiteStore(str(tmp_path / "test.db"))
    await store.initialize()
    async with store.connect() as conn:
        yield conn


# ---------------------------------------------------------------------------
# SkillGenome.maturity_tag + SkillCandidate.fast_track DB roundtrip
# ---------------------------------------------------------------------------

class TestSchemaExtensions:
    async def test_maturity_tag_roundtrip(self, db_conn):
        from loom.core.memory.procedural import ProceduralMemory

        proc = ProceduralMemory(db_conn)
        skill = SkillGenome(name="roundtrip-skill", body="# test", maturity_tag="mature")
        await proc.upsert(skill)

        fetched = await proc.get("roundtrip-skill")
        assert fetched is not None
        assert fetched.maturity_tag == "mature"

    async def test_update_maturity_tag(self, db_conn):
        from loom.core.memory.procedural import ProceduralMemory

        proc = ProceduralMemory(db_conn)
        skill = SkillGenome(name="mt-skill", body="# test")
        await proc.upsert(skill)

        updated = await proc.update_maturity_tag("mt-skill", "needs_improvement")
        assert updated is True

        fetched = await proc.get("mt-skill")
        assert fetched is not None
        assert fetched.maturity_tag == "needs_improvement"

    async def test_update_maturity_tag_clear(self, db_conn):
        from loom.core.memory.procedural import ProceduralMemory

        proc = ProceduralMemory(db_conn)
        skill = SkillGenome(name="mt2-skill", body="# test", maturity_tag="mature")
        await proc.upsert(skill)

        await proc.update_maturity_tag("mt2-skill", None)
        fetched = await proc.get("mt2-skill")
        assert fetched is not None
        assert fetched.maturity_tag is None

    async def test_candidate_fast_track_roundtrip(self, db_conn):
        from loom.core.memory.procedural import ProceduralMemory

        proc = ProceduralMemory(db_conn)
        skill = SkillGenome(name="ft-skill", body="# test")
        await proc.upsert(skill)

        candidate = SkillCandidate(
            parent_skill_name="ft-skill",
            parent_version=1,
            candidate_body="# improved",
            mutation_strategy="batch_meta_skill_engineer",
            fast_track=True,
        )
        await proc.insert_candidate(candidate)

        fetched = await proc.get_candidate(candidate.id)
        assert fetched is not None
        assert fetched.fast_track is True

    async def test_candidate_fast_track_default_false(self, db_conn):
        from loom.core.memory.procedural import ProceduralMemory

        proc = ProceduralMemory(db_conn)
        skill = SkillGenome(name="ft2-skill", body="# test")
        await proc.upsert(skill)

        candidate = SkillCandidate(
            parent_skill_name="ft2-skill",
            parent_version=1,
            candidate_body="# improved",
            mutation_strategy="apply_suggestions",
        )
        await proc.insert_candidate(candidate)

        fetched = await proc.get_candidate(candidate.id)
        assert fetched is not None
        assert fetched.fast_track is False
