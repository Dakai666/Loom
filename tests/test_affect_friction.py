"""
P1 affect arm — ``environment_friction`` contract tests (epic #528, issue #487,
spec docs/designs/60 §4). Written red-first.

P1 builds *metabolism*, not emotion: a decaying arousal reading whose only
injection source is prediction error, surfaced solely through a deterministic
Critic note. These tests pin the invariants (I3/I5/I6), the three consumer
contracts (C1–C3 + D7's C2-a/C2-b), the metabolism (S1/T1/W1/W2/M1–M3), state
continuity (P1) and the exits (O1/O2).
"""

from __future__ import annotations

import hashlib
import inspect
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from loom.core.cognition import affect
from loom.core.cognition.affect import (
    AffectState,
    N0,
    compute_surprise,
    load_state,
    read_surprise,
    render_friction_note,
    save_state,
    settle_friction,
)
from loom.core.memory.prediction import PredictionRecord, PredictionStore
from loom.core.memory.semantic import SemanticMemory
from loom.core.memory.store import SQLiteStore

NOW = datetime(2026, 9, 16, 1, 5, tzinfo=UTC)


def rec(domain, score, *, hours_ago=1.0, reconciled_hours_ago=0.05, explicit=False):
    return PredictionRecord(
        session_id="s1",
        claim="bet",
        due_condition={"kind": "after_action", "call_id": "c"},
        resolver={"kind": "duration_bucket", "expect": "fast"},
        domain=domain,
        context="explicit:predict_tool" if explicit else "auto:implicit_latency",
        status="reconciled",
        score=score,
        created_at=NOW - timedelta(hours=hours_ago),
        reconciled_at=NOW - timedelta(hours=reconciled_hours_ago),
    )


def history(domain, score, n, *, days_ago=5):
    """Old reconciled records that establish a domain's standing baseline."""
    return [
        rec(domain, score, hours_ago=24 * days_ago + i, reconciled_hours_ago=24 * days_ago)
        for i in range(n)
    ]


def surprise_of(corpus, window, *, since=NOW - timedelta(hours=24)):
    return compute_surprise(corpus + window, window, since=since, now=NOW)


def contribution(reading, domain):
    return next((d.surprise for d in reading.environment.drivers if d.domain == domain), 0.0)


@pytest_asyncio.fixture
async def db(tmp_path):
    store = SQLiteStore(str(tmp_path / "affect.db"))
    await store.initialize()
    async with store.connect() as conn:
        yield conn


def _call(tool_name, args):
    from loom.core.harness.middleware import ToolCall
    from loom.core.harness.permissions import TrustLevel
    return ToolCall(tool_name=tool_name, args=args, trust_level=TrustLevel.SAFE, session_id="s1")


async def seed(db, records):
    """Bets are born pending (I4); stamp the settled fields directly so tests
    control event and settle times."""
    ps = PredictionStore(db)
    for r in records:
        settled = (r.status, r.score, r.reconciled_at)
        r.status, r.score, r.reconciled_at = "pending", None, None
        await ps.write(r)
        await db.execute(
            "UPDATE prediction_records SET status = ?, score = ?, reconciled_at = ? WHERE id = ?",
            (settled[0], settled[1], settled[2].isoformat(), r.id),
        )
    await db.commit()


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


class TestI3SpineIsReadOnly:
    async def _spine_digest(self, db):
        h = hashlib.sha256()
        cur = await db.execute("SELECT * FROM prediction_records ORDER BY id")
        for row in await cur.fetchall():
            h.update(repr(row).encode())
        cur = await db.execute(
            "SELECT key, value, confidence FROM semantic_entries "
            "WHERE key LIKE 'calibration:%' ORDER BY key"
        )
        for row in await cur.fetchall():
            h.update(repr(row).encode())
        return h.hexdigest()

    async def test_settle_commit_leaves_spine_untouched(self, db):
        await seed(db, history("read_file@latency", 0.0, 20)
                   + [rec("read_file@latency", 1.0, hours_ago=h) for h in (1, 2, 3)])
        from loom.core.memory.semantic import SemanticEntry
        await SemanticMemory(db).upsert(SemanticEntry(
            key="calibration:read_file@latency", value="calibration[x]", confidence=1.0,
        ))
        before = await self._spine_digest(db)
        await settle_friction(db, now=NOW, commit=True)
        assert await self._spine_digest(db) == before


class TestI5CriticIsTheOnlyExit:
    def test_only_the_critic_renders_text(self):
        """No public callable other than the Critic returns prompt-injectable str."""
        str_returning = set()
        for name, obj in vars(affect).items():
            if name.startswith("_") or not inspect.isfunction(obj):
                continue
            if getattr(obj, "__module__", None) != affect.__name__:
                continue
            ret = inspect.signature(obj).return_annotation
            if ret in (str, "str"):
                str_returning.add(name)
        assert str_returning == {"render_friction_note"}

    def test_tarot_draw_is_independent_of_affect_state(self):
        skill_dir = Path(__file__).resolve().parents[1] / "skills" / "sisi_mood_tarot"
        if not skill_dir.exists():
            pytest.skip("tarot skill not present")
        sys.path.insert(0, str(skill_dir))
        try:
            import daily_draw
        finally:
            sys.path.remove(str(skill_dir))
        source = inspect.getsource(daily_draw)
        assert "affect" not in source and "friction" not in source
        d = date(2026, 9, 16)
        assert daily_draw.daily_draw(d) == daily_draw.daily_draw(d)


class TestI6NoSentimentChannel:
    @pytest.mark.parametrize("fn,allowed", [
        (read_surprise, {"db", "since", "now"}),
        (compute_surprise, {"corpus", "window", "since", "now"}),
        (settle_friction, {"db", "now", "commit"}),
        (AffectState.absorb, {"self", "reading"}),
    ])
    def test_signatures_carry_no_sentiment(self, fn, allowed):
        assert set(inspect.signature(fn).parameters) == allowed


# ---------------------------------------------------------------------------
# Consumer contracts
# ---------------------------------------------------------------------------


class TestC1HealthNotResidue:
    async def test_residue_alone_yields_empty_reading(self, db):
        from loom.core.memory.semantic import SemanticEntry
        await SemanticMemory(db).upsert(SemanticEntry(
            key="calibration:memorize@latency", value="calibration[x]: score=0.10",
            confidence=0.1,
        ))
        reading = await read_surprise(db, since=NOW - timedelta(hours=24), now=NOW)
        assert reading.environment.drivers == []
        assert reading.environment.injection == 0.0


class TestWindowBounds:
    async def test_window_excludes_settles_after_now(self, db):
        await seed(db, history("read_file@latency", 0.0, 50)
                   + [rec("read_file@latency", 1.0, hours_ago=-5, reconciled_hours_ago=-6)])
        reading = await read_surprise(db, since=NOW - timedelta(hours=24), now=NOW)
        assert reading.environment.drivers == []


class TestC2Exclusions:
    def test_thin_domain_contributes_nothing(self):
        corpus = history("rare@latency", 0.0, 3)  # n < SAMPLE_FLOOR overall
        window = [rec("rare@latency", 1.0)]
        assert contribution(surprise_of(corpus, window), "rare@latency") == 0.0

    def test_c2a_low_info_domain_is_a_deviation_baseline(self):
        corpus = history("read_file@latency", 0.0, 100)  # LOW_INFORMATION
        window = [rec("read_file@latency", 1.0, hours_ago=h) for h in range(1, 16)]
        window += [rec("read_file@latency", 0.0, hours_ago=h + 0.5) for h in range(1, 16)]
        assert contribution(surprise_of(corpus, window), "read_file@latency") > 0.0

    def test_c2a_note_never_states_capability(self):
        corpus = history("read_file@latency", 0.0, 100)
        window = [rec("read_file@latency", 1.0, hours_ago=h) for h in range(1, 6)]
        reading = surprise_of(corpus, window)
        note = render_friction_note(AffectState().absorb(reading), reading)
        assert "calibration" not in note and "score" not in note

    def test_c2b_one_slip_imperceptible_sustained_visible(self):
        corpus = history("read_file@latency", 0.0, 100)
        normal = [rec("read_file@latency", 0.0, hours_ago=h / 2) for h in range(1, 31)]
        one_slip = normal + [rec("read_file@latency", 1.0, hours_ago=3)]
        sustained = normal + [rec("read_file@latency", 1.0, hours_ago=h / 2 + 0.1)
                              for h in range(1, 16)]
        slip_state = AffectState().absorb(surprise_of(corpus, one_slip))
        sustained_state = AffectState().absorb(surprise_of(corpus, sustained))
        assert round(slip_state.environment, 2) <= 0.03
        assert sustained_state.environment >= 0.2


class TestC3EffectiveMonoculture:
    def test_sparse_explicit_renders_model_na(self):
        corpus = history("run_bash@latency", 0.1, 4997)
        corpus += [rec("memory", 0.0, hours_ago=100, explicit=True) for _ in range(3)]
        window = [rec("memory", 1.0, explicit=True)]
        reading = surprise_of(corpus, window)
        assert reading.model.injection is None
        state = AffectState().absorb(reading)
        assert state.model is None
        note = render_friction_note(state, reading)
        assert "model: n/a" in note and "model: 0.00" not in note

    def test_monoculture_flag_semantics_unchanged(self):
        from loom.core.cognition.calibration import compute_calibration
        from loom.core.cognition.calibration_health import assess_calibration_health
        corpus = history("run_bash@latency", 0.1, 50)
        corpus += [rec("memory", 0.0, hours_ago=100, explicit=True)]
        health = assess_calibration_health(compute_calibration(corpus), corpus)
        assert health.monoculture is False  # binary explicit==0 rule, not the 1% floor

    def test_dense_explicit_feeds_model_track(self):
        corpus = history("memory", 0.0, 50)
        corpus = [
            PredictionRecord(**{**r.__dict__, "context": "explicit:predict_tool"}) for r in corpus
        ] + history("run_bash@latency", 0.1, 100)
        window = [rec("memory", 1.0, hours_ago=h, explicit=True) for h in range(1, 11)]
        reading = surprise_of(corpus, window)
        assert reading.model.injection is not None and reading.model.injection > 0
        assert contribution(reading, "memory") == 0.0  # explicit never feeds environment


# ---------------------------------------------------------------------------
# Metabolism
# ---------------------------------------------------------------------------


class TestMetabolism:
    def test_s1_deviation_not_absolute(self):
        corpus = history("dream_cycle@latency", 1.0, 34)
        window = [rec("dream_cycle@latency", 1.0)]
        assert contribution(surprise_of(corpus, window), "dream_cycle@latency") == pytest.approx(0.0)

    def test_t1_aging_uses_event_time(self):
        """Reconcile batches at dawn: identical reconciled_at, different event time."""
        corpus = history("write_file@latency", 0.0, 50) + history("fetch_url@latency", 0.0, 50)
        fresh = [rec("write_file@latency", 0.0, hours_ago=20) for _ in range(5)]
        fresh += [rec("write_file@latency", 1.0, hours_ago=0.2)]
        stale = [rec("fetch_url@latency", 0.0, hours_ago=0.2) for _ in range(5)]
        stale += [rec("fetch_url@latency", 1.0, hours_ago=20)]
        reading = surprise_of(corpus, fresh + stale)
        assert contribution(reading, "write_file@latency") > contribution(reading, "fetch_url@latency")

    def test_w1_domains_weighted_equally_not_by_volume(self):
        corpus = history("run_bash@latency", 0.0, 600) + history("recall@latency", 0.0, 50)
        big = [rec("run_bash@latency", 1.0 if i % 2 else 0.0, hours_ago=0.5) for i in range(600)]
        small = [rec("recall@latency", 1.0 if i % 2 else 0.0, hours_ago=0.5) for i in range(40)]
        reading = surprise_of(corpus, big + small)
        ratio = contribution(reading, "run_bash@latency") / contribution(reading, "recall@latency")
        assert ratio < 1.15  # only shrinkage separates them, never volume

    def test_w2_single_record_capped_by_shrinkage(self):
        corpus = history("send_discord_file@latency", 0.0, 20)
        window = [rec("send_discord_file@latency", 1.0, hours_ago=0.01)]
        c = contribution(surprise_of(corpus, window), "send_discord_file@latency")
        assert c <= 1.0 / (1 + N0) + 1e-9

    def test_m1_decays_monotonically_to_zero(self):
        s = AffectState(environment=0.8, model=None, t0=NOW)
        values = [s.decayed(NOW + timedelta(hours=h)).environment for h in (0, 6, 12, 48, 400)]
        assert values == sorted(values, reverse=True)
        assert values[2] == pytest.approx(0.4, rel=1e-3)
        assert 0.0 <= values[-1] < 1e-6

    def test_m2_clamped(self):
        corpus = []
        window = []
        for i in range(40):
            d = f"tool{i}@latency"
            corpus += history(d, 0.0, 20)
            window += [rec(d, 1.0, hours_ago=0.1) for _ in range(20)]
        state = AffectState(environment=0.9, t0=NOW).absorb(surprise_of(corpus, window))
        assert state.environment == 1.0

    async def test_m3_no_double_injection(self, db):
        await seed(db, history("read_file@latency", 0.0, 50)
                   + [rec("read_file@latency", 1.0, hours_ago=h) for h in (1, 2, 3, 4)])
        first, _ = await settle_friction(db, now=NOW, commit=True)
        second, reading = await settle_friction(db, now=NOW, commit=True)
        assert first.environment > 0
        assert reading.environment.injection == 0.0
        assert second.environment == pytest.approx(first.environment)


# ---------------------------------------------------------------------------
# Persistence & exits
# ---------------------------------------------------------------------------


class TestPersistence:
    async def test_p1_state_survives_restart_with_decay(self, db):
        await save_state(db, AffectState(environment=0.6, model=None, t0=NOW))
        loaded = await load_state(db)
        assert loaded.environment == pytest.approx(0.6)
        assert loaded.t0 == NOW
        assert loaded.decayed(NOW + timedelta(hours=12)).environment == pytest.approx(0.3, rel=1e-3)

    async def test_state_lives_in_memory_meta_not_semantic(self, db):
        await save_state(db, AffectState(environment=0.2, t0=NOW))
        cur = await db.execute("SELECT value FROM memory_meta WHERE key = ?", (affect.META_KEY,))
        assert (await cur.fetchone()) is not None
        cur = await db.execute("SELECT COUNT(*) FROM semantic_entries WHERE key LIKE 'affect%'")
        assert (await cur.fetchone())[0] == 0

    async def test_corrupt_state_reads_as_fresh(self, db):
        await db.execute(
            "INSERT INTO memory_meta(key, value, updated_at) VALUES (?, ?, ?)",
            (affect.META_KEY, "{not json", NOW.isoformat()),
        )
        await db.commit()
        assert (await load_state(db)).environment == 0.0


class TestExits:
    def test_o1_note_always_rendered_even_when_quiet(self):
        reading = compute_surprise([], [], since=NOW - timedelta(hours=24), now=NOW)
        note = render_friction_note(AffectState(), reading)
        assert note.startswith("<environment_friction>")
        for field in ("window:", "environment: 0.00", "model: n/a", "attribution:", "confidence:"):
            assert field in note

    def test_note_has_no_interpreting_prose(self):
        reading = compute_surprise([], [], since=NOW - timedelta(hours=24), now=NOW)
        note = render_friction_note(AffectState(), reading)
        for word in ("moderate", "quiet", "不是你", "should", "feel"):
            assert word not in note

    async def test_o2_affect_read_defaults_to_dry_run(self, db):
        from loom.core.memory.maintenance import make_affect_read_tool

        await seed(db, history("read_file@latency", 0.0, 50)
                   + [rec("read_file@latency", 1.0, hours_ago=h) for h in (1, 2, 3)])
        await save_state(db, AffectState(environment=0.1, t0=NOW - timedelta(hours=24)))
        tool = make_affect_read_tool(db, clock=lambda: NOW)
        result = await tool.executor(_call("affect_read", {}))
        assert result.success and "<environment_friction>" in result.output
        assert (await load_state(db)).environment == pytest.approx(0.1)

        await tool.executor(_call("affect_read", {"dry_run": False}))
        assert (await load_state(db)).t0 == NOW

    async def test_reconcile_tool_appends_note_when_enabled(self, db, tmp_path):
        from loom.core.memory.maintenance import make_prediction_reconcile_tool

        on = make_prediction_reconcile_tool(db, dreams_dir=tmp_path, friction_note=True)
        off = make_prediction_reconcile_tool(db, dreams_dir=tmp_path)
        r_on = await on.executor(_call("prediction_reconcile", {}))
        r_off = await off.executor(_call("prediction_reconcile", {}))
        assert "<environment_friction>" in r_on.output
        assert "<environment_friction>" not in r_off.output
