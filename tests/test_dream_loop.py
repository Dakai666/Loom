"""Daemon distill-dream loop (#613 E, P3) — replaces the 12 h themed dream.

The daemon's ``"dream"`` loop now runs ``run_distill_dream`` twice a day on
the restart-safe cadence the convergent dream uses (``last_run`` persisted in
``memory_meta`` under its own key), seeding each pass from the themed domain
rotation. Writes need ``[memory.distill_dream] execute = true`` — the same
gate as the tool. The 03:00 free dream is a separate schedule and untouched.
"""

from __future__ import annotations

import logging
import types
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from loom.autonomy.maintenance import ConvergentDreamLoop
from loom.core.infra import AbortController
from loom.core.memory.store import SQLiteStore


@pytest_asyncio.fixture
async def db(tmp_path):
    store = SQLiteStore(str(tmp_path / "loop.db"))
    await store.initialize()
    async with store.connect() as conn:
        yield conn


# ── cadence: same loop as the convergent dream, own meta key ───────────────

class TestCadence:
    async def test_distill_and_consolidation_cadences_are_independent(self, db):
        ran = []

        async def distill():
            ran.append("distill")

        async def consolidate():
            ran.append("consolidate")
        now = datetime(2026, 10, 19, tzinfo=UTC)
        d = ConvergentDreamLoop(dream_fn=distill, db=db, abort=AbortController(),
                                interval_days=0.5, meta_key="distill_dream.last_run")
        c = ConvergentDreamLoop(dream_fn=consolidate, db=db, abort=AbortController(),
                                interval_days=7.0)
        assert await d.maybe_run_once(now)
        assert await c.maybe_run_once(now)          # distill's run didn't mark it
        assert not await d.maybe_run_once(now + timedelta(hours=11))
        assert await d.maybe_run_once(now + timedelta(hours=12))
        assert not await c.maybe_run_once(now + timedelta(hours=12))
        assert ran == ["distill", "consolidate", "distill"]

    def test_check_interval_is_configurable(self, db):
        loop = ConvergentDreamLoop(dream_fn=None, db=db, abort=AbortController(),
                                   interval_days=0.5, check_interval_seconds=1800.0)
        assert loop._check_interval_seconds == 1800.0

    def test_consolidation_defaults_unchanged(self, db):
        loop = ConvergentDreamLoop(dream_fn=None, db=db, abort=AbortController())
        assert loop._meta_key == "consolidation_dream.last_run"
        assert loop._check_interval_seconds == 6 * 3600.0


# ── daemon wiring ───────────────────────────────────────────────────────────

def _daemon(session):
    from loom.autonomy.daemon import AutonomyDaemon
    return AutonomyDaemon(notify_router=None, confirm_flow=None, loom_session=session)


def _session(db):
    return types.SimpleNamespace(
        _db=db, _memory=types.SimpleNamespace(semantic=object()),
        router=object(), model="m",
    )


async def _wire(monkeypatch, db, cfg: dict):
    """Start the loop with *cfg* as the whole [memory] table; return
    (dream_fn, loop kwargs, captured run_distill_dream kwargs, reports)."""
    import loom.autonomy.circadian.journal as journal_mod
    import loom.autonomy.daemon as daemon_mod
    import loom.core.cognition.distillation as distill_mod
    import loom.core.cognition.dreaming as dreaming_mod
    import loom.core.config as config_mod

    captured: dict = {}
    reports: list[str] = []

    async def fake_run(semantic, llm_fn, **kw):
        captured.update(kw)
        captured.setdefault("domains", []).append(kw.get("domain"))
        if captured.pop("fail_next", False):
            raise RuntimeError("provider down")
        plan = types.SimpleNamespace(
            pass_id="distill-x", domain=kw.get("domain"), mode="read-only",
            counts=lambda: {"approved": 0}, outcome_counts=lambda: {}, backup_failed=False,
        )
        return plan, "distill report"

    async def no_dream_cycle(**_):
        raise AssertionError("the dream loop must not run dream_cycle any more")

    monkeypatch.setattr(distill_mod, "run_distill_dream", fake_run)
    monkeypatch.setattr(dreaming_mod, "dream_cycle", no_dream_cycle)
    def fake_append(report, **_):
        if captured.get("journal_broken"):
            raise OSError("disk full")
        reports.append(report)
    monkeypatch.setattr(journal_mod, "append_distill_report", fake_append)
    monkeypatch.setattr(config_mod, "load_loom_config", lambda: {"memory": cfg})
    holder: dict = {}

    class FakeLoop:
        def __init__(self, **kw):
            holder.update(kw)
            self._interval_seconds = kw.get("interval_days", 7.0) * 86400.0

        def run_forever(self):
            async def _noop():
                return None
            return _noop()

    monkeypatch.setattr(daemon_mod, "ConvergentDreamLoop", FakeLoop)
    daemon = _daemon(_session(db))
    task = daemon._maybe_start_dream_loop()
    holder["daemon"] = daemon
    return task, holder, captured, reports


class TestWiring:
    async def test_runs_a_distill_pass_and_journals_it(self, monkeypatch, db):
        task, loop_kw, captured, reports = await _wire(monkeypatch, db, {})
        assert task is not None
        await loop_kw["dream_fn"]()
        assert reports == ["distill report"]
        assert captured["execute"] is False                     # gate defaults off
        assert loop_kw["meta_key"] == "distill_dream.last_run"
        assert loop_kw["interval_days"] == pytest.approx(0.5)   # 12 h

    async def test_domain_rotates_across_passes(self, monkeypatch, db):
        _, loop_kw, captured, _ = await _wire(monkeypatch, db, {})
        seen = []
        for _ in range(5):
            await loop_kw["dream_fn"]()
            seen.append(captured["domain"])
        assert seen == ["self", "user", "project", "knowledge", "self"]

    async def test_knobs_come_from_config(self, monkeypatch, db):
        _, loop_kw, captured, _ = await _wire(monkeypatch, db, {"distill_dream": {
            "execute": True, "interval_hours": 6, "max_neighbourhoods": 8,
            "min_similarity": 0.72, "corpus_limit": 20000,
        }})
        await loop_kw["dream_fn"]()
        assert captured["execute"] is True
        assert captured["max_neighbourhoods"] == 8
        assert captured["min_similarity"] == pytest.approx(0.72)
        assert captured["corpus_limit"] == 20000
        assert loop_kw["interval_days"] == pytest.approx(0.25)

    @pytest.mark.parametrize("value", ["true", 1, "yes"])
    async def test_execute_needs_a_literal_true(self, monkeypatch, db, value):
        _, loop_kw, captured, _ = await _wire(
            monkeypatch, db, {"distill_dream": {"execute": value}})
        await loop_kw["dream_fn"]()
        assert captured["execute"] is False

    @pytest.mark.parametrize("key, bad", [
        ("max_neighbourhoods", 0), ("max_neighbourhoods", "five"),
        ("corpus_limit", -1), ("min_similarity", 1.5), ("min_similarity", "x"),
        ("interval_hours", 0),
    ])
    async def test_bad_knob_falls_back_with_a_warning(self, monkeypatch, db, caplog, key, bad):
        from loom.core.cognition.distillation import (
            DEFAULT_MAX_NEIGHBOURHOODS, DISTILL_MIN_SIMILARITY,
        )
        with caplog.at_level(logging.WARNING):
            _, loop_kw, captured, _ = await _wire(
                monkeypatch, db, {"distill_dream": {key: bad}})
            await loop_kw["dream_fn"]()
        assert key in caplog.text
        assert captured["max_neighbourhoods"] == DEFAULT_MAX_NEIGHBOURHOODS
        assert captured["min_similarity"] == pytest.approx(DISTILL_MIN_SIMILARITY)
        assert captured["corpus_limit"] == 15000
        assert loop_kw["interval_days"] == pytest.approx(0.5)

    async def test_disabled_starts_nothing(self, monkeypatch, db):
        task, *_ = await _wire(monkeypatch, db, {"distill_dream": {"enabled": False}})
        assert task is None

    async def test_retired_dream_section_is_ignored_with_a_warning(self, monkeypatch, db, caplog):
        with caplog.at_level(logging.WARNING):
            task, loop_kw, _, reports = await _wire(
                monkeypatch, db, {"dream": {"enabled": True, "interval_hours": 3}})
            await loop_kw["dream_fn"]()
        assert "[memory.dream]" in caplog.text
        assert loop_kw["interval_days"] == pytest.approx(0.5)   # not the old 3 h
        assert reports == ["distill report"]                    # distill, not dream_cycle

    def test_no_session_or_db_starts_nothing(self):
        assert _daemon(None)._maybe_start_dream_loop() is None
        assert _daemon(_session(None))._maybe_start_dream_loop() is None


class TestReviewFixes:
    async def test_old_opt_out_is_honoured(self, monkeypatch, db):
        # someone who switched the themed dream off didn't opt into a new one
        task, *_ = await _wire(monkeypatch, db, {"dream": {"enabled": False}})
        assert task is None

    async def test_new_section_wins_over_old_opt_out(self, monkeypatch, db):
        task, *_ = await _wire(monkeypatch, db, {
            "dream": {"enabled": False}, "distill_dream": {"enabled": True}})
        assert task is not None

    async def test_journal_failure_does_not_rerun_the_pass(self, monkeypatch, db, caplog):
        _, loop_kw, captured, _ = await _wire(monkeypatch, db, {})
        captured["journal_broken"] = True
        with caplog.at_level(logging.ERROR):
            await loop_kw["dream_fn"]()             # must not raise → last_run gets marked
        assert "distill report" in caplog.text      # the report survives in the log

    async def test_failed_pass_keeps_its_domain(self, monkeypatch, db):
        _, loop_kw, captured, _ = await _wire(monkeypatch, db, {})
        captured["fail_next"] = True
        with pytest.raises(RuntimeError):
            await loop_kw["dream_fn"]()
        await loop_kw["dream_fn"]()
        assert captured["domains"] == ["self", "self"]

    async def test_rotation_is_independent_of_the_dream_cycle_tool(self, monkeypatch, db):
        from loom.core.cognition.dreaming import next_themed_domain
        _, loop_kw, captured, _ = await _wire(monkeypatch, db, {})
        await loop_kw["dream_fn"]()
        await next_themed_domain(db)                # dream_cycle(themed=True) via the tool
        await next_themed_domain(db)
        await loop_kw["dream_fn"]()
        assert captured["domains"] == ["self", "user"]

    async def test_fractional_interval_is_accepted(self, monkeypatch, db):
        _, loop_kw, *_ = await _wire(monkeypatch, db, {"distill_dream": {"interval_hours": 6.5}})
        assert loop_kw["interval_days"] == pytest.approx(6.5 / 24)

    @pytest.mark.parametrize("bad", [0, -1, "x", True])
    async def test_bad_interval_falls_back(self, monkeypatch, db, bad):
        _, loop_kw, *_ = await _wire(monkeypatch, db, {"distill_dream": {"interval_hours": bad}})
        assert loop_kw["interval_days"] == pytest.approx(0.5)

    async def test_distill_and_consolidation_passes_never_overlap(self, monkeypatch, db):
        import asyncio

        import loom.autonomy.circadian.journal as journal_mod
        import loom.core.cognition.consolidation as consolidation_mod
        import loom.core.cognition.distillation as distill_mod
        import loom.autonomy.daemon as daemon_mod
        import loom.core.config as config_mod

        events = []

        def plan():
            return types.SimpleNamespace(
                pass_id="p", domain=None, mode="read-only", counts=lambda: {},
                outcome_counts=lambda: {}, backup_failed=False, deferred_to_next_pass=0)

        async def slow(tag):
            events.append(f"{tag}+")
            await asyncio.sleep(0.05)
            events.append(f"{tag}-")
            return plan(), "r"

        async def run_distill(*a, **k):
            return await slow("d")

        async def run_convergent(*a, **k):
            return await slow("c")
        monkeypatch.setattr(distill_mod, "run_distill_dream", run_distill)
        monkeypatch.setattr(consolidation_mod, "run_convergent_dream", run_convergent)
        monkeypatch.setattr(journal_mod, "append_distill_report", lambda r, **_: None)
        monkeypatch.setattr(journal_mod, "append_consolidation_report", lambda r, **_: None)
        monkeypatch.setattr(config_mod, "load_loom_config", lambda: {"memory": {
            "consolidation_dream": {"enabled": True}}})
        fns = []

        class FakeLoop:
            def __init__(self, **kw):
                fns.append(kw["dream_fn"])
                self._interval_seconds = 1.0

            def run_forever(self):
                async def _noop():
                    return None
                return _noop()
        monkeypatch.setattr(daemon_mod, "ConvergentDreamLoop", FakeLoop)
        daemon = _daemon(_session(db))
        daemon._maybe_start_dream_loop()
        daemon._maybe_start_consolidation_loop()
        await asyncio.gather(*(fn() for fn in fns))
        assert events in (["d+", "d-", "c+", "c-"], ["c+", "c-", "d+", "d-"])


class TestRealClosure:
    """The real daemon closure against a real store — only the LLM is faked
    (the stubbed tests above would accept any kwargs; #451's crash hid there)."""

    async def test_execute_pass_writes_and_journals(self, monkeypatch, db, tmp_path):
        import loom.autonomy.circadian.journal as journal_mod
        import loom.autonomy.daemon as daemon_mod
        import loom.core.config as config_mod
        from loom.core.memory.semantic import SemanticMemory
        from tests.test_distill_dream import _llm, _topic

        await _topic(db)
        fake_llm = _llm()

        class Router:
            async def chat(self, *, model, messages, max_tokens):
                return types.SimpleNamespace(text=await fake_llm(messages))

        real_append = journal_mod.append_distill_report
        monkeypatch.setattr(journal_mod, "append_distill_report",
                            lambda r, **kw: real_append(r, dreams_dir=tmp_path / "dreams"))
        monkeypatch.setattr(config_mod, "load_loom_config", lambda: {"memory": {
            "distill_dream": {"execute": True}}})
        holder = {}

        class FakeLoop:
            def __init__(self, **kw):
                holder.update(kw)
                self._interval_seconds = 1.0

            def run_forever(self):
                async def _noop():
                    return None
                return _noop()
        monkeypatch.setattr(daemon_mod, "ConvergentDreamLoop", FakeLoop)
        session = types.SimpleNamespace(
            _db=db, _memory=types.SimpleNamespace(semantic=SemanticMemory(db)),
            router=Router(), model="m")
        from loom.core.cognition.dreaming import mark_themed_domain
        # the fixture topic lives in "knowledge" — the domain after "project"
        await mark_themed_domain(db, "distill_dream.last_theme", "project")
        assert _daemon(session)._maybe_start_dream_loop() is not None
        plan = await holder["dream_fn"]()
        assert plan.domain == "knowledge"
        assert plan.outcome_counts()["executed"] == 1
        (report,) = list((tmp_path / "dreams").glob("*.md"))
        assert "記憶蒸餾" in report.read_text()
