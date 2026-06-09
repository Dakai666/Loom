"""
Prediction Spine — P0 slice 4: calibration residue + surprise three quantities
(epic #528, spec docs/designs/58 §3.2/§6; decay tier #530 = Option A).

Slice 4 turns reconciled bets into the spine's actual *residue*: a rolling
per-domain ``calibration:<domain>`` semantic summary, plus the three read-only
surprise quantities (``error_score`` / ``uncertainty`` / ``appraisal_valence``,
§6). This is the P0 acceptance gate — the first non-trivial, repeatable,
auditable signal that a frozen-weights agent accumulated world-model calibration
in the framework layer.

Contract (TDD-first), guarding the lifeline invariants:

* **I4** — only ``reconciled`` predictions (with a real ``score``) feed
  calibration; ``pending`` / ``due`` / ``stale`` never count (unreconciled ≠
  verified).
* **I6** — calibration moves ONLY from prediction-vs-observation scores. There
  is no parameter, and no path, by which user sentiment can shift it.
* **I3 / I5** — the three quantities are inert read-only data on the summary.
  The module writes the calibration fact and *nothing else* (no mood, no Critic,
  no surprise push). The write is behind its own ``execute`` gate, not smuggled
  into reconcile's gate.
* **#530** — calibration is a *normal* semantic fact (``temporal=recent``,
  ``domain=knowledge`` — the world-model-residue framing of §3.2). Its rolling
  re-write keeps ``updated_at`` fresh, so an actively-bet domain never reaches
  the half-life; an abandoned one decays. Dry-run writes nothing.
"""

from datetime import datetime, UTC, timedelta

import pytest
import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.core.memory.semantic import SemanticMemory
from loom.core.memory.prediction import PredictionRecord
from loom.core.memory.ontology import DOMAIN_KNOWLEDGE, TEMPORAL_RECENT
from loom.core.cognition.calibration import (
    CalibrationSummary,
    compute_calibration,
    write_calibration,
    SAMPLE_FLOOR,
)


# ---------------------------------------------------------------------------
# helpers — construct reconciled / unreconciled records directly
# ---------------------------------------------------------------------------

def _reconciled(domain="cli", *, score, expect=True, kind="tool_success"):
    resolver = {"kind": kind}
    if kind in ("tool_success", "output_contains", "output_regex", "file_digest_changed"):
        resolver["expect"] = expect
    return PredictionRecord(
        session_id="s",
        claim="bet",
        due_condition={"kind": "after_action", "call_id": "c"},
        resolver=resolver,
        domain=domain,
        status="reconciled",
        score=score,
        observation_ref="action:a1",
    )


def _pending(domain="cli"):
    return PredictionRecord(
        session_id="s", claim="bet",
        due_condition={"kind": "after_action", "call_id": "c"},
        resolver={"kind": "tool_success", "expect": True},
        domain=domain,
    )


# ---------------------------------------------------------------------------
# I4 — only reconciled predictions feed calibration
# ---------------------------------------------------------------------------

class TestI4OnlyReconciledCounts:
    def test_pending_due_stale_excluded(self):
        records = [
            _reconciled("cli", score=0.0),
            _reconciled("cli", score=1.0),
            _pending("cli"),                                   # excluded
            PredictionRecord(session_id="s", claim="b",
                             due_condition={"kind": "after_action", "call_id": "c"},
                             resolver={"kind": "tool_success"}, domain="cli",
                             status="due"),                    # excluded
            PredictionRecord(session_id="s", claim="b",
                             due_condition={"kind": "after_action", "call_id": "c"},
                             resolver={"kind": "tool_success"}, domain="cli",
                             status="stale"),                  # excluded
        ]
        summaries = compute_calibration(records)
        assert len(summaries) == 1
        assert summaries[0].domain == "cli"
        assert summaries[0].n == 2          # only the two reconciled

    def test_reconciled_without_score_is_not_counted(self):
        """A 'reconciled' status but score is None is malformed — never count it
        as a free perfect bet (no-silent-pass, pairs with I4)."""
        bad = _reconciled("cli", score=0.0)
        bad.score = None
        summaries = compute_calibration([bad])
        assert summaries == []

    def test_empty_input_is_empty_output(self):
        assert compute_calibration([]) == []


# ---------------------------------------------------------------------------
# Aggregation — per-domain mean error + calibration score
# ---------------------------------------------------------------------------

class TestAggregation:
    def test_grouped_per_domain(self):
        records = [
            _reconciled("cli", score=0.0),
            _reconciled("cli", score=0.0),
            _reconciled("git", score=1.0),
        ]
        by_domain = {s.domain: s for s in compute_calibration(records)}
        assert set(by_domain) == {"cli", "git"}
        assert by_domain["cli"].error_score == 0.0
        assert by_domain["cli"].calibration_score == 1.0
        assert by_domain["git"].error_score == 1.0
        assert by_domain["git"].calibration_score == 0.0

    def test_mean_error(self):
        s = compute_calibration([
            _reconciled("cli", score=0.0),
            _reconciled("cli", score=1.0),
            _reconciled("cli", score=0.5),
        ])[0]
        assert s.error_score == pytest.approx(0.5)
        assert s.calibration_score == pytest.approx(0.5)

    def test_key_is_calibration_prefixed(self):
        s = compute_calibration([_reconciled("cli", score=0.0)])[0]
        assert s.key == "calibration:cli"


# ---------------------------------------------------------------------------
# §6 — the three surprise quantities
# ---------------------------------------------------------------------------

class TestUncertainty:
    def test_high_when_few_samples(self):
        """Sample insufficiency: one bet is not enough to know a domain."""
        s = compute_calibration([_reconciled("cli", score=0.0)])[0]
        # perfectly calibrated (mean_error 0) but n=1 ⇒ still uncertain
        expected_insuff = (SAMPLE_FLOOR - 1) / SAMPLE_FLOOR
        assert s.uncertainty == pytest.approx(expected_insuff)

    def test_high_when_often_wrong(self):
        """Miscalibration drives uncertainty even with plenty of samples."""
        records = [_reconciled("cli", score=0.9) for _ in range(SAMPLE_FLOOR + 5)]
        s = compute_calibration(records)[0]
        assert s.uncertainty == pytest.approx(0.9)   # mean_error dominates

    def test_low_when_well_sampled_and_accurate(self):
        records = [_reconciled("cli", score=0.0) for _ in range(SAMPLE_FLOOR + 5)]
        s = compute_calibration(records)[0]
        assert s.uncertainty == pytest.approx(0.0)


class TestAppraisalValence:
    def test_negative_on_surprising_failure(self):
        """Predicted success, got failure (error 1) → negative surprise."""
        s = compute_calibration([_reconciled("cli", score=1.0, expect=True)])[0]
        assert s.appraisal_valence == pytest.approx(-1.0)

    def test_positive_on_surprising_success(self):
        """Bet it would FAIL (expect=False), it succeeded → positive surprise."""
        s = compute_calibration([_reconciled("cli", score=1.0, expect=False)])[0]
        assert s.appraisal_valence == pytest.approx(1.0)

    def test_zero_on_expected_outcome(self):
        """A correctly predicted outcome (error 0) carries no surprise."""
        s = compute_calibration([_reconciled("cli", score=0.0, expect=True)])[0]
        assert s.appraisal_valence == pytest.approx(0.0)

    def test_zero_for_non_polar_resolver(self):
        """P0 valence is conservative: only resolvers with a mechanically
        unambiguous success pole (tool_success) carry valence. row_count etc.
        get polarity 0 — the spine does not invent a value judgment (I5)."""
        s = compute_calibration([
            _reconciled("db", score=1.0, kind="row_count"),
        ])[0]
        assert s.appraisal_valence == pytest.approx(0.0)
        # ...but it still counts toward error_score / calibration
        assert s.error_score == pytest.approx(1.0)


class TestQuantityRanges:
    def test_all_quantities_in_range(self):
        records = [
            _reconciled("cli", score=0.0, expect=True),
            _reconciled("cli", score=1.0, expect=True),
            _reconciled("cli", score=0.3, expect=False),
        ]
        s = compute_calibration(records)[0]
        assert 0.0 <= s.error_score <= 1.0
        assert 0.0 <= s.uncertainty <= 1.0
        assert -1.0 <= s.appraisal_valence <= 1.0
        assert 0.0 <= s.calibration_score <= 1.0


# ---------------------------------------------------------------------------
# I6 — calibration derives ONLY from prediction-vs-observation
# ---------------------------------------------------------------------------

class TestI6NoSentimentPath:
    def test_compute_signature_has_no_sentiment_input(self):
        """Structural I6: the only input is prediction records. There is no
        parameter through which user sentiment could enter."""
        import inspect
        params = list(inspect.signature(compute_calibration).parameters)
        assert params == ["records"]

    def test_only_observation_scored_records_move_calibration(self):
        """A domain whose bets are all unreconciled (no observation-derived
        score) produces no calibration — sentiment about it is irrelevant."""
        summaries = compute_calibration([_pending("vibes"), _pending("vibes")])
        assert summaries == []


# ---------------------------------------------------------------------------
# Write gate — I3 / I5 / #530 Option A
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_calibration.db")


@pytest_asyncio.fixture
async def store(tmp_db):
    s = SQLiteStore(tmp_db)
    await s.initialize()
    return s


@pytest_asyncio.fixture
async def semantic(store):
    async with store.connect() as db:
        yield SemanticMemory(db)


class TestWriteGate:
    async def test_dry_run_writes_nothing(self, semantic):
        """I3: the default pass is read-only — computes but does not persist."""
        summaries = compute_calibration([_reconciled("cli", score=0.0)])
        written = await write_calibration(semantic, summaries, execute=False)
        assert written == []
        assert await semantic.get("calibration:cli") is None

    async def test_execute_writes_calibration_fact(self, semantic):
        summaries = compute_calibration([
            _reconciled("cli", score=0.0),
            _reconciled("cli", score=0.0),
        ])
        written = await write_calibration(semantic, summaries, execute=True)
        assert written == ["calibration:cli"]
        entry = await semantic.get("calibration:cli")
        assert entry is not None

    async def test_written_fact_is_knowledge_recent(self, semantic):
        """#530 Option A: world-model residue lands as normal semantic
        (domain=knowledge, temporal=recent) — NOT a milestone permanence hack."""
        summaries = compute_calibration([_reconciled("cli", score=0.0)])
        await write_calibration(semantic, summaries, execute=True)
        entry = await semantic.get("calibration:cli")
        assert entry.domain == DOMAIN_KNOWLEDGE
        assert entry.temporal == TEMPORAL_RECENT

    async def test_confidence_reflects_calibration(self, semantic):
        """A well-calibrated domain is a high-confidence fact; a poorly
        calibrated one is low-confidence."""
        good = compute_calibration([_reconciled("cli", score=0.0) for _ in range(6)])
        bad = compute_calibration([_reconciled("git", score=1.0) for _ in range(6)])
        await write_calibration(semantic, good + bad, execute=True)
        assert (await semantic.get("calibration:cli")).confidence == pytest.approx(1.0)
        assert (await semantic.get("calibration:git")).confidence == pytest.approx(0.0, abs=0.05)

    async def test_quantities_persisted_in_metadata(self, semantic):
        """The three quantities ride along in metadata so P1/P2 consumers can
        read them later without recomputing (read-only broadcast, §6)."""
        summaries = compute_calibration([_reconciled("cli", score=1.0, expect=True)])
        await write_calibration(semantic, summaries, execute=True)
        meta = (await semantic.get("calibration:cli")).metadata
        assert "error_score" in meta
        assert "uncertainty" in meta
        assert "appraisal_valence" in meta
        assert meta["n"] == 1

    async def test_rolling_rewrite_is_stable_key(self, semantic):
        """The summary is *rolling*: a second pass overwrites the same key
        rather than accumulating duplicates — this is what keeps an active
        domain's updated_at fresh (#530 mechanism)."""
        await write_calibration(
            semantic, compute_calibration([_reconciled("cli", score=1.0)]), execute=True)
        await write_calibration(
            semantic, compute_calibration([_reconciled("cli", score=0.0)]), execute=True)
        entry = await semantic.get("calibration:cli")
        # latest pass won; still one row under the stable key
        assert entry.confidence == pytest.approx(1.0)  # score 0.0 → calibration 1.0


# ---------------------------------------------------------------------------
# #530 — the verifiable decay subtest (Option A)
# ---------------------------------------------------------------------------

class TestDecayBehaviorOptionA:
    """The decision: calibration is normal semantic. An *actively* re-written
    domain never decays (its anchor stays fresh); an *abandoned* one fades.
    Asserted against the lifecycle mechanism directly."""

    def test_active_domain_survives_abandoned_decays(self):
        from loom.core.memory.lifecycle import effective_confidence
        now = datetime.now(UTC)
        # active: the dream re-wrote it today → anchor fresh
        active = effective_confidence(
            0.9, now, now, DOMAIN_KNOWLEDGE, TEMPORAL_RECENT)
        # abandoned: last written ~300d ago, never recalled since
        old = now - timedelta(days=300)
        abandoned = effective_confidence(
            0.9, old, None, DOMAIN_KNOWLEDGE, TEMPORAL_RECENT)
        assert active == pytest.approx(0.9, abs=0.01)   # untouched by decay
        assert abandoned < active
        assert abandoned < 0.15                          # crossed toward prune
