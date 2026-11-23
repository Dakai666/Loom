"""#690 S5a — trigger fire records.

Contract:
- A running daemon persists every cron fire to ``trigger_history``, so a
  restart inside the same minute does not fire the trigger twice.
- Every fire of a cron, an anchor or a memory loop lands in the ledger as one
  ``trigger_fire`` event: fired_at / started_at / ended_at / outcome.
- Anchors tell spawned, delivered and missed apart; missed carries a reason,
  both in the ledger and in ``phase_log``.
- A cron minute the evaluator never looked at is recorded as missed.

Only external I/O is stubbed: the chime delivery callback and the Discord
platform. The memory DB and the ledger are real SQLite files under tmp_path.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from loom.autonomy.circadian import state as st
from loom.autonomy.circadian.lifecycle import (
    CircadianConfig,
    ensure_today_session,
    register_rhythm_anchors,
    register_triggers,
)
from loom.autonomy.circadian.rhythm import Anchor
from loom.autonomy.circadian.state import CircadianState, state_lock
from loom.autonomy.evaluator import TriggerEvaluator
from loom.autonomy.history import TriggerHistory
from loom.autonomy.triggers import CronTrigger
from loom.core.ledger import LedgerEmitter
from loom.core.ledger.store import LedgerStore
from loom.core.memory.store import SQLiteStore

UTC = timezone.utc
TZ = "Asia/Taipei"
CFG = CircadianConfig(enabled=True, timezone=TZ, start="08:00", sleep="00:00")


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def mem_db(tmp_path):
    store = SQLiteStore(str(tmp_path / "memory.db"))
    await store.initialize()
    async with store.connect() as conn:
        yield conn


@pytest_asyncio.fixture
async def ledger(tmp_path):
    store = LedgerStore(db_path=tmp_path / "ledger.db")
    await store.open()
    yield store
    await store.close()


@pytest.fixture(autouse=True)
def _circadian_isolated(tmp_path, monkeypatch):
    st.set_dir_for_test(tmp_path / "circadian")
    monkeypatch.chdir(tmp_path)
    yield
    st.set_dir_for_test(None)


def _session(mem_db, ledger):
    return types.SimpleNamespace(
        _db=mem_db,
        _ledger_emitter=LedgerEmitter(ledger, session_id="autonomy-session"),
        _memory=None,
    )


def _daemon(session, delivery=None):
    from loom.autonomy.daemon import AutonomyDaemon
    return AutonomyDaemon(
        notify_router=None, confirm_flow=None, loom_session=session,
        chime_delivery=delivery,
    )


async def _events(ledger, event_type):
    return [
        e for e in await ledger.fetch_by_turn("system:autonomy")
        if e.event_type == event_type
    ]


async def _fire_records(ledger):
    return [e.payload for e in await _events(ledger, "trigger_fire")]


class FakePlatform:
    def __init__(self):
        self.threads: dict[int, bool] = {}
        self._next = 5000

    def resolve_channel_id(self):
        return 777

    async def spawn_daily_thread(self, *, name, channel_id):
        tid = self._next
        self._next += 1
        self.threads[tid] = True
        return tid, f"session-{tid}"

    async def verify_thread_alive(self, thread_id):
        return self.threads.get(thread_id, False)

    async def ensure_session_loaded(self, thread_id):
        pass

    async def close_daily_session(self, thread_id):
        pass


class NoChannelPlatform(FakePlatform):
    def resolve_channel_id(self):
        return None


# ── 1. restart dedup through trigger_history ───────────────────────────────

class TestRestartDedup:
    async def test_fire_survives_a_restart_in_the_same_minute(self, mem_db, ledger):
        runs: list[str] = []

        async def handler(trigger, context):
            runs.append(trigger.name)

        first = _daemon(_session(mem_db, ledger))
        first.evaluator.register(CronTrigger(name="daily", intent="x", cron="0 9 * * *"))
        first.register_direct_handler("daily", handler)
        at = datetime(2026, 11, 23, 9, 0, 5, tzinfo=UTC)
        assert await first.evaluator.evaluate_cron(at) == ["daily"]
        assert await TriggerHistory(mem_db).get_last_fire("daily") == at

        # The process restarts inside the same minute.
        second = _daemon(_session(mem_db, ledger))
        second.evaluator.register(CronTrigger(name="daily", intent="x", cron="0 9 * * *"))
        second.register_direct_handler("daily", handler)
        assert await second.evaluator.evaluate_cron(at + timedelta(seconds=40)) == []
        assert runs == ["daily"]

    async def test_yesterdays_fire_does_not_suppress_today(self, mem_db, ledger):
        # 10:00 on the 5th and 08:20 on the 6th used to share one minute key
        # (day*100 + hour*60 + minute), so the restored history row
        # suppressed a fire that was due.
        cron = "0,20 8,10 * * *"
        await TriggerHistory(mem_db).record_fire(
            "twice", datetime(2026, 11, 5, 10, 0, tzinfo=UTC)
        )
        daemon = _daemon(_session(mem_db, ledger))
        daemon.evaluator.register(CronTrigger(name="twice", intent="x", cron=cron))
        daemon.register_direct_handler("twice", _noop)
        fired = await daemon.evaluator.evaluate_cron(
            datetime(2026, 11, 6, 8, 20, tzinfo=UTC)
        )
        assert fired == ["twice"]


async def _noop(trigger, context):
    return None


# ── 2. one fire record per fire ────────────────────────────────────────────

class TestFireRecord:
    async def test_cron_fire_records_times_and_outcome(self, mem_db, ledger):
        daemon = _daemon(_session(mem_db, ledger))
        daemon.evaluator.register(CronTrigger(name="daily", intent="x", cron="0 9 * * *"))
        daemon.register_direct_handler("daily", _noop)
        await daemon.evaluator.evaluate_cron(datetime(2026, 11, 23, 9, 0, tzinfo=UTC))

        [rec] = await _fire_records(ledger)
        assert rec["name"] == "daily"
        assert rec["source"] == "cron"
        assert rec["outcome"] == "completed"
        assert rec["reason"] is None
        assert rec["fired_at"] <= rec["started_at"] <= rec["ended_at"]

    async def test_record_shares_the_fire_observation_correlation(self, mem_db, ledger):
        daemon = _daemon(_session(mem_db, ledger))
        daemon.evaluator.register(CronTrigger(name="daily", intent="x", cron="0 9 * * *"))
        daemon.register_direct_handler("daily", _noop)
        await daemon.evaluator.evaluate_cron(datetime(2026, 11, 23, 9, 0, tzinfo=UTC))

        [obs] = await _events(ledger, "env_observation")
        [rec] = await _events(ledger, "trigger_fire")
        assert obs.payload["detail"]["trigger_name"] == "daily"
        assert rec.correlation_id == obs.correlation_id

    async def test_raising_reaction_records_failed(self, mem_db, ledger):
        async def boom(trigger, context):
            raise RuntimeError("provider down")

        daemon = _daemon(_session(mem_db, ledger))
        daemon.evaluator.register(CronTrigger(name="daily", intent="x", cron="0 9 * * *"))
        daemon.register_direct_handler("daily", boom)
        await daemon.evaluator.evaluate_cron(datetime(2026, 11, 23, 9, 0, tzinfo=UTC))

        [rec] = await _fire_records(ledger)
        assert rec["outcome"] == "failed"
        assert rec["reason"] == "RuntimeError"
        assert rec["ended_at"] is not None

    async def test_planner_chime_without_a_live_target_is_missed(
        self, mem_db, ledger, monkeypatch,
    ):
        from loom.autonomy.planner import ActionDecision, PlannedAction
        from loom.core.harness.permissions import TrustLevel

        async def nobody_home(req):
            return False

        daemon = _daemon(_session(mem_db, ledger), delivery=nobody_home)

        async def plan(trigger, context):
            return PlannedAction(
                trigger_name=trigger.name, intent="x", prompt="x",
                decision=ActionDecision.EXECUTE, trust_level=TrustLevel.SAFE,
                context={"mode": "chime", "target": {"type": "discord_thread", "id": 1}},
            )

        monkeypatch.setattr(daemon._planner, "handle", plan)
        daemon.evaluator.register(CronTrigger(name="nudge", intent="x", cron="0 9 * * *"))
        await daemon.evaluator.evaluate_cron(datetime(2026, 11, 23, 9, 0, tzinfo=UTC))

        [rec] = await _fire_records(ledger)
        assert (rec["outcome"], rec["reason"]) == ("missed", "no_target_session")

    async def test_daemon_without_session_still_fires(self):
        from loom.autonomy.daemon import AutonomyDaemon
        runs = []

        async def handler(trigger, context):
            runs.append(trigger.name)

        daemon = AutonomyDaemon(notify_router=None, confirm_flow=None)
        daemon.evaluator.register(CronTrigger(name="daily", intent="x", cron="0 9 * * *"))
        daemon.register_direct_handler("daily", handler)
        await daemon.evaluator.evaluate_cron(datetime(2026, 11, 23, 9, 0, tzinfo=UTC))
        assert runs == ["daily"]


# ── 3. anchors: spawned / delivered / missed ───────────────────────────────

def _anchor_daemon(mem_db, ledger, accept: bool):
    async def delivery(req):
        return accept
    daemon = _daemon(_session(mem_db, ledger), delivery=delivery)
    register_rhythm_anchors(daemon, CFG, [Anchor(time="08:30", name="dawn", meaning="x")])
    return daemon


async def _today_session_with_empty_log():
    await ensure_today_session(datetime.now(UTC), FakePlatform(), CFG)
    state = CircadianState.load()
    with state_lock():
        state.phase_log.clear()
        state.save_atomic()


def _anchor(daemon):
    return next(t for t in daemon.evaluator.list() if t.name == "circadian:phase_dawn")


class TestAnchorOutcomes:
    async def test_delivered(self, mem_db, ledger):
        daemon = _anchor_daemon(mem_db, ledger, accept=True)
        await _today_session_with_empty_log()
        await daemon.evaluator._fire(_anchor(daemon), {})

        [rec] = await _fire_records(ledger)
        assert rec["source"] == "cron"
        assert (rec["outcome"], rec["reason"]) == ("delivered", None)
        assert CircadianState.load().phase_log[-1]["outcome"] == "delivered"

    async def test_undeliverable_chime_is_missed_with_reason(self, mem_db, ledger):
        daemon = _anchor_daemon(mem_db, ledger, accept=False)
        await _today_session_with_empty_log()
        await daemon.evaluator._fire(_anchor(daemon), {})

        [rec] = await _fire_records(ledger)
        assert (rec["outcome"], rec["reason"]) == ("missed", "no_today_session")
        entry = CircadianState.load().phase_log[-1]
        assert (entry["outcome"], entry["reason"]) == ("missed", "no_today_session")

    async def test_missed_before_any_session_still_reaches_the_ledger(self, mem_db, ledger):
        # #656: the anchor fired before today's session existed. phase_log has
        # nowhere to write; the ledger record is the trace.
        daemon = _anchor_daemon(mem_db, ledger, accept=False)
        await daemon.evaluator._fire(_anchor(daemon), {})

        [rec] = await _fire_records(ledger)
        assert rec["outcome"] == "missed"
        assert CircadianState.load() is None

    async def test_dawn_spawn_records_spawned_then_completed(self, mem_db, ledger):
        daemon = _daemon(_session(mem_db, ledger))
        register_triggers(daemon, FakePlatform(), CFG)
        spawn = next(t for t in daemon.evaluator.list() if t.name == "circadian:dawn_spawn")

        await daemon.evaluator._fire(spawn, {})   # creates today's session
        await daemon.evaluator._fire(spawn, {})   # today's session already there

        outcomes = [r["outcome"] for r in await _fire_records(ledger)]
        assert outcomes == ["spawned", "completed"]

    async def test_dawn_spawn_without_a_channel_is_missed(self, mem_db, ledger):
        daemon = _daemon(_session(mem_db, ledger))
        register_triggers(daemon, NoChannelPlatform(), CFG)
        spawn = next(t for t in daemon.evaluator.list() if t.name == "circadian:dawn_spawn")
        await daemon.evaluator._fire(spawn, {})

        [rec] = await _fire_records(ledger)
        assert (rec["outcome"], rec["reason"]) == ("missed", "no_session")


# ── 4. minutes the evaluator never looked at ───────────────────────────────

class TestEvaluatorGap:
    async def test_minutes_skipped_by_a_blocked_evaluator_are_reported(self):
        missed: list[tuple[str, datetime]] = []

        async def on_missed(trigger, due_at):
            missed.append((trigger.name, due_at))

        ev = TriggerEvaluator(on_missed=on_missed)
        ev.register(CronTrigger(name="half_past", intent="x", cron="30 8 * * *"))
        ev.register(CronTrigger(name="nine", intent="x", cron="0 9 * * *"))
        day = datetime(2026, 10, 29, 8, 0, tzinfo=UTC)

        await ev.evaluate_cron(day)                          # 08:00
        await ev.evaluate_cron(day + timedelta(minutes=47))  # blocked until 08:47
        assert missed == [("half_past", day + timedelta(minutes=30))]

    async def test_consecutive_minutes_report_nothing(self):
        missed = []

        async def on_missed(trigger, due_at):
            missed.append(trigger.name)

        ev = TriggerEvaluator(on_missed=on_missed)
        ev.register(CronTrigger(name="every", intent="x", cron="* * * * *"))
        day = datetime(2026, 10, 29, 8, 0, 59, tzinfo=UTC)
        await ev.evaluate_cron(day)
        await ev.evaluate_cron(day + timedelta(seconds=2))   # 08:01:01
        assert missed == []

    async def test_first_evaluation_reports_nothing(self):
        missed = []

        async def on_missed(trigger, due_at):
            missed.append(trigger.name)

        ev = TriggerEvaluator(on_missed=on_missed)
        ev.register(CronTrigger(name="early", intent="x", cron="0 1 * * *"))
        await ev.evaluate_cron(datetime(2026, 10, 29, 8, 0, tzinfo=UTC))
        assert missed == []

    async def test_blocked_anchor_is_missed_in_ledger_and_phase_log(self, mem_db, ledger):
        deliveries = []

        async def delivery(req):
            deliveries.append(req)
            return True

        daemon = _daemon(_session(mem_db, ledger), delivery=delivery)
        # Local 08:30 in Taipei is 00:30 UTC.
        register_rhythm_anchors(daemon, CFG, [Anchor(time="08:30", name="dawn", meaning="x")])
        await _today_session_with_empty_log()
        day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

        await daemon.evaluator.evaluate_cron(day)
        await daemon.evaluator.evaluate_cron(day + timedelta(minutes=47))

        assert deliveries == []
        [rec] = await _fire_records(ledger)
        assert rec["name"] == "circadian:phase_dawn"
        assert (rec["outcome"], rec["reason"]) == ("missed", "evaluator_gap")
        assert rec["fired_at"] == (day + timedelta(minutes=30)).timestamp()
        assert rec["started_at"] is None and rec["ended_at"] is None
        entry = CircadianState.load().phase_log[-1]
        assert (entry["phase"], entry["outcome"], entry["reason"]) == (
            "dawn", "missed", "evaluator_gap",
        )


# ── 5. memory loops share the record ───────────────────────────────────────

class TestMemoryLoops:
    async def test_dream_pass_records_completed_and_failed(self, mem_db, ledger):
        from loom.autonomy.fire_record import FireLog
        from loom.autonomy.maintenance import ConvergentDreamLoop
        from loom.core.infra import AbortController

        fire_log = FireLog(LedgerEmitter(ledger, session_id="s"))
        calls = []

        async def dream():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("provider down")

        loop = ConvergentDreamLoop(
            dream_fn=dream, db=mem_db, abort=AbortController(),
            interval_days=0.5, meta_key="distill_dream.last_run",
            label="distill-dream", fire_log=fire_log,
        )
        now = datetime.now(UTC) - timedelta(days=2)
        assert await loop.maybe_run_once(now) is True
        assert await loop.maybe_run_once(now + timedelta(hours=1)) is False  # not due
        with pytest.raises(RuntimeError):
            await loop.maybe_run_once(now + timedelta(hours=13))

        recs = await _fire_records(ledger)
        assert [(r["source"], r["name"], r["outcome"]) for r in recs] == [
            ("memory_loop", "distill-dream", "completed"),
            ("memory_loop", "distill-dream", "failed"),
        ]
        assert recs[1]["reason"] == "RuntimeError"
        assert all(r["fired_at"] <= r["started_at"] <= r["ended_at"] for r in recs)

    async def test_maintenance_sweep_records_completed_then_throttled(self, mem_db, ledger):
        from loom.autonomy.fire_record import FireLog
        from loom.autonomy.maintenance import MaintenanceLoop
        from loom.core.infra import AbortController

        loop = MaintenanceLoop(
            db=mem_db, abort=AbortController(), min_gap_minutes=30.0,
            fire_log=FireLog(LedgerEmitter(ledger, session_id="s")),
        )
        await loop.run_once()
        await loop.run_once()   # inside the 30-minute throttle

        recs = await _fire_records(ledger)
        assert [(r["name"], r["outcome"], r["reason"]) for r in recs] == [
            ("memory_maintenance", "completed", None),
            ("memory_maintenance", "skipped", "throttle"),
        ]

    async def test_daemon_wires_its_fire_log_into_the_dream_loops(
        self, monkeypatch, mem_db, ledger,
    ):
        import loom.autonomy.daemon as daemon_mod
        import loom.core.config as config_mod
        from loom.autonomy.maintenance import ConvergentDreamLoop

        monkeypatch.setattr(config_mod, "load_loom_config", lambda: {
            "memory": {"consolidation_dream": {"enabled": True}},
        })
        built: dict = {}

        class Capture(ConvergentDreamLoop):
            def __init__(self, **kw):
                super().__init__(**kw)
                built["loop"] = self

            def run_forever(self):
                async def _idle():
                    return None
                return _idle()

        monkeypatch.setattr(daemon_mod, "ConvergentDreamLoop", Capture)
        session = _session(mem_db, ledger)
        session._memory = types.SimpleNamespace(semantic=object())
        session.router, session.model = object(), "m"
        daemon = _daemon(session)
        task = daemon._maybe_start_consolidation_loop()
        await task

        loop = built["loop"]
        loop._dream_fn = _no_dream
        await loop.maybe_run_once(datetime.now(UTC))
        [rec] = await _fire_records(ledger)
        assert (rec["name"], rec["outcome"]) == ("consolidation", "completed")


async def _no_dream():
    return None
