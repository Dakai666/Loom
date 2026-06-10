"""
Prediction Spine — calibration self-monitoring (epic #528, P0.5-b, #539).

The immune system: a *read-only* analyzer over the spine's own
``calibration:<domain>`` residue that flags "did it write something
meaningless?" so DK can let go without reading audit reports by hand. It writes
nothing, drives no behaviour, needs no gate (report-only is safe), and invents
no value judgment (I5) — its only inputs are calibration summaries and the
reconciled records that produced them (no sentiment channel, I6).

Contract pinned here (red first):

* **low_information** (the headline, 絲絲 #538) — a domain whose calibration sits
  ≈1.0 with flat valence and enough sample is *high but uninformative*: the tool
  just succeeds, that isn't insight. It must be told apart from genuine signal.
* **sample_insufficient** — ``n < SAMPLE_FLOOR``: the score is statistically
  unstable; the report says so explicitly rather than letting a consumer read a
  noisy 1.0.
* **genuine_signal** — calibration meaningfully off 1.0, or non-flat valence:
  the domains a consumer should actually attend to.
* **monoculture** (corpus-level) — every reconciled bet is implicit (``auto:``),
  none explicit: calibration is measuring tool reliability, not prediction
  skill. This is *why* a slice-B-only phase flatlines, and it self-clears once
  explicit wagers flow.
* **I6** — the analyzer's signature admits only (summaries, records); there is
  no parameter through which user sentiment could enter.
"""

import inspect

import pytest
import pytest_asyncio

from loom.core.cognition.calibration import CalibrationSummary, SAMPLE_FLOOR, run_calibration_pass
from loom.core.cognition.calibration_health import (
    assess_calibration_health,
    CalibrationHealthReport,
    DomainHealth,
    LOW_INFORMATION,
    SAMPLE_INSUFFICIENT,
    GENUINE_SIGNAL,
)
from loom.core.memory.prediction import PredictionRecord, PredictionStore
from loom.core.memory.store import SQLiteStore
from loom.core.memory.semantic import SemanticMemory


def _summary(domain, *, n=10, error=0.0, valence=0.0):
    """A CalibrationSummary with the fields the analyzer reads."""
    return CalibrationSummary(
        domain=domain,
        n=n,
        error_score=error,
        uncertainty=max(error, max(0.0, (SAMPLE_FLOOR - n) / SAMPLE_FLOOR)),
        appraisal_valence=valence,
        calibration_score=1.0 - error,
    )


def _rec(*, domain="run_bash", context="auto:implicit_tool_success",
         status="reconciled", score=0.0):
    return PredictionRecord(
        session_id="s", claim="c",
        due_condition={"kind": "after_action", "call_id": "x"},
        resolver={"kind": "tool_success", "expect": True},
        domain=domain, context=context, status=status, score=score,
    )


def _by_domain(report, domain):
    return next(d for d in report.domains if d.domain == domain)


# ---------------------------------------------------------------------------
# Per-domain classification
# ---------------------------------------------------------------------------

class TestLowInformation:
    def test_flat_near_one_is_low_information(self):
        """The headline: high calibration + flat valence + enough sample =
        uninformative, not insight."""
        report = assess_calibration_health([_summary("run_bash", n=20, error=0.0, valence=0.0)], [])
        assert _by_domain(report, "run_bash").classification == LOW_INFORMATION

    def test_low_information_listed_separately_from_genuine(self):
        report = assess_calibration_health([
            _summary("run_bash", n=20, error=0.0, valence=0.0),       # flat
            _summary("git_push", n=20, error=0.4, valence=-0.3),      # signal
        ], [])
        assert {d.domain for d in report.low_information()} == {"run_bash"}
        assert {d.domain for d in report.genuine()} == {"git_push"}

    def test_high_score_but_non_flat_valence_is_genuine(self):
        """≈1.0 calibration but a directional surprise lean is still signal —
        not every near-perfect domain is uninformative."""
        report = assess_calibration_health([_summary("fetch", n=20, error=0.02, valence=0.4)], [])
        assert _by_domain(report, "fetch").classification == GENUINE_SIGNAL


class TestSampleInsufficient:
    def test_thin_sample_flagged_regardless_of_score(self):
        report = assess_calibration_health([_summary("rare_tool", n=2, error=0.0, valence=0.0)], [])
        assert _by_domain(report, "rare_tool").classification == SAMPLE_INSUFFICIENT

    def test_sample_insufficiency_takes_priority_over_low_information(self):
        """A thin-sample near-1.0 domain is 'unstable', not 'flatlined' — you
        can't call it uninformative when you barely sampled it."""
        report = assess_calibration_health([_summary("rare", n=1, error=0.0, valence=0.0)], [])
        d = _by_domain(report, "rare")
        assert d.classification == SAMPLE_INSUFFICIENT
        assert d.classification != LOW_INFORMATION


class TestGenuineSignal:
    def test_meaningfully_off_one_is_genuine(self):
        report = assess_calibration_health([_summary("model_of_dk", n=12, error=0.5, valence=-0.2)], [])
        assert _by_domain(report, "model_of_dk").classification == GENUINE_SIGNAL


# ---------------------------------------------------------------------------
# Corpus-level monoculture
# ---------------------------------------------------------------------------

class TestMonoculture:
    def test_all_auto_bets_is_monoculture(self):
        records = [_rec(context="auto:implicit_tool_success") for _ in range(10)]
        report = assess_calibration_health([_summary("run_bash")], records)
        assert report.monoculture is True

    def test_any_explicit_bet_clears_monoculture(self):
        records = [_rec(context="auto:implicit_tool_success") for _ in range(9)]
        records.append(_rec(context="explicit:predict_tool"))
        report = assess_calibration_health([_summary("run_bash")], records)
        assert report.monoculture is False

    def test_too_few_reconciled_is_not_monoculture(self):
        """Below the sample floor, diversity is unjudgeable — don't cry wolf."""
        records = [_rec() for _ in range(SAMPLE_FLOOR - 1)]
        report = assess_calibration_health([_summary("run_bash")], records)
        assert report.monoculture is False

    def test_summaries_with_empty_records_flag_monoculture_false(self):
        """絲絲 PR #542 P2: a caller that computes summaries then hands records=[]
        (the natural shape if the two are fetched separately) must not crash and
        must not cry monoculture — diversity is unjudgeable with no records, while
        each domain still classifies by its own n/score/valence."""
        report = assess_calibration_health([
            _summary("run_bash", n=20, error=0.0, valence=0.0),   # low_information
            _summary("git_push", n=20, error=0.4, valence=-0.3),  # genuine
        ], [])
        assert report.monoculture is False
        assert "too few" in report.monoculture_detail
        assert _by_domain(report, "run_bash").classification == LOW_INFORMATION
        assert _by_domain(report, "git_push").classification == GENUINE_SIGNAL

    def test_monoculture_ignores_unreconciled_records(self):
        """Only reconciled bets count toward the diversity judgment (I4-adjacent):
        a pile of pending explicit bets must not 'clear' monoculture before they
        actually settle."""
        records = [_rec(context="auto:implicit_tool_success") for _ in range(10)]
        records += [_rec(context="explicit:predict_tool", status="pending", score=None)
                    for _ in range(5)]
        report = assess_calibration_health([_summary("run_bash")], records)
        assert report.monoculture is True


# ---------------------------------------------------------------------------
# Empty corpus + report shape
# ---------------------------------------------------------------------------

class TestEmptyAndShape:
    def test_empty_corpus_is_inert(self):
        report = assess_calibration_health([], [])
        assert report.domains == []
        assert report.monoculture is False
        assert isinstance(report.render(), str)

    def test_render_and_to_dict(self):
        report = assess_calibration_health([
            _summary("run_bash", n=20, error=0.0, valence=0.0),
            _summary("git_push", n=20, error=0.4, valence=-0.3),
        ], [_rec() for _ in range(10)])
        text = report.render()
        assert "run_bash" in text and "git_push" in text
        d = report.to_dict()
        assert d["counts"]["low_information"] == 1
        assert d["counts"]["genuine_signal"] == 1
        assert d["monoculture"] is True


# ---------------------------------------------------------------------------
# I6 — structural no-sentiment guarantee
# ---------------------------------------------------------------------------

class TestNoSentimentPath:
    def test_signature_admits_only_summaries_and_records(self):
        params = list(inspect.signature(assess_calibration_health).parameters)
        assert params == ["summaries", "records"]


# ---------------------------------------------------------------------------
# Integration — run_calibration_pass carries the health verdict (read-only)
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_cal_health.db")


@pytest_asyncio.fixture
async def store(tmp_db):
    s = SQLiteStore(tmp_db)
    await s.initialize()
    return s


async def _seed_reconciled(ps, domain, *, score, n, context="auto:implicit_tool_success"):
    for _ in range(n):
        pred = PredictionRecord(
            session_id="s", claim="bet",
            due_condition={"kind": "after_action", "call_id": "c"},
            resolver={"kind": "tool_success", "expect": True},
            domain=domain, context=context,
        )
        await ps.write(pred)
        await ps.mark_reconciled(pred.id, score=score, observation_ref="action:a1")


class TestPassIntegration:
    async def test_pass_attaches_health_even_in_dry_run(self, store):
        """The immune verdict is read-only — it must ride the report regardless
        of the write gate, so a dry-run (calibration_write off) journal shows it."""
        async with store.connect() as db:
            ps, sem = PredictionStore(db), SemanticMemory(db)
            await _seed_reconciled(ps, "run_bash", score=0.0, n=8)  # flat near-1.0

            report = await run_calibration_pass(ps, sem, execute=False)
            assert report.written is False
            assert report.health is not None
            assert report.health.monoculture is True
            assert report.health.low_information()[0].domain == "run_bash"
            assert "calibration health" in report.render()

    async def test_explicit_bets_clear_monoculture_in_pass(self, store):
        async with store.connect() as db:
            ps, sem = PredictionStore(db), SemanticMemory(db)
            await _seed_reconciled(ps, "run_bash", score=0.0, n=6)
            await _seed_reconciled(ps, "git", score=0.4, n=6,
                                   context="explicit:predict_tool")
            report = await run_calibration_pass(ps, sem, execute=False)
            assert report.health.monoculture is False
