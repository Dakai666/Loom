"""
Tests for the circadian lifecycle (issue #459, doc/57 §5 robustness table).

Exercises ``ensure_today_session`` idempotence + recovery, nightly ``close_today``,
startup reconciliation, the dawn/close trigger wiring, and the daemon's
direct-handler bypass — all against a fake ``CircadianPlatform`` so no Discord
connection is needed. State IO is redirected to a tmp dir.

Robustness rows covered here:
  - daemon first start (no state)            → spawn
  - daemon restart same-day (thread alive)   → no re-spawn, resume
  - manual thread delete (thread gone)       → rebuild
  - daemon restart cross-day (unclosed)      → recover + spawn
  - cross-day startup, already closed        → archive only
  - double-daemon spawn race                 → state lock arbitration (state test)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from loom.autonomy.circadian import state as st
from loom.autonomy.circadian.state import CircadianState, state_lock
from loom.autonomy.circadian.lifecycle import (
    CircadianConfig,
    close_today,
    ensure_today_session,
    is_in_active_hours,
    local_hhmm_to_utc_cron,
    recover_on_startup,
    register_triggers,
    setup_circadian,
)

TZ = "Asia/Taipei"
CFG = CircadianConfig(enabled=True, timezone=TZ, start="08:00", sleep="00:00")
# A config that is "active" at every wall-clock minute, for setup_circadian tests.
CFG_ALWAYS = CircadianConfig(enabled=True, timezone=TZ, start="00:00", sleep="00:00")


@pytest.fixture(autouse=True)
def _circ_dir(tmp_path):
    st.set_dir_for_test(tmp_path / "circadian")
    yield
    st.set_dir_for_test(None)


class FakeEvaluator:
    def __init__(self):
        self.emitted: list[tuple[str, dict]] = []
        self.registered: list = []

    async def emit(self, name, payload):
        self.emitted.append((name, dict(payload)))

    def register(self, trigger):
        self.registered.append(trigger)

    def names(self):
        return [t.name for t in self.registered]


class FakePlatform:
    """In-memory CircadianPlatform. Records every adapter call."""

    def __init__(self, channel_id: int | None = 999):
        self._channel_id = channel_id
        self.threads: dict[int, bool] = {}     # thread_id → alive
        self.loaded: list[int] = []
        self.closed: list[int] = []
        self.spawns: list[tuple[str, int]] = []
        self._next = 1000
        self.spawn_should_raise = False

    def resolve_channel_id(self):
        return self._channel_id

    async def spawn_daily_thread(self, *, name, channel_id):
        if self.spawn_should_raise:
            raise RuntimeError("discord create_thread failed")
        tid = self._next
        self._next += 1
        self.threads[tid] = True
        self.spawns.append((name, channel_id))
        return tid, f"session-{tid}"

    async def verify_thread_alive(self, thread_id):
        return self.threads.get(thread_id, False)

    async def ensure_session_loaded(self, thread_id):
        self.loaded.append(thread_id)

    async def close_daily_session(self, thread_id):
        self.closed.append(thread_id)
        self.threads[thread_id] = False


def _yesterday() -> str:
    return (datetime.now(ZoneInfo(TZ)) - timedelta(days=1)).strftime("%Y-%m-%d")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ===========================================================================
# ensure_today_session
# ===========================================================================


class TestEnsureSpawn:
    async def test_first_run_spawns_and_writes_state(self):
        plat, ev = FakePlatform(), FakeEvaluator()
        state = await ensure_today_session(_now_utc(), plat, CFG, evaluator=ev)

        assert state is not None
        assert len(plat.spawns) == 1
        assert plat.spawns[0][0].startswith("daily-life-")
        persisted = CircadianState.load()
        assert persisted is not None and persisted.thread_id == state.thread_id
        assert ("circadian:day_started", {
            "date": state.date,
            "thread_id": state.thread_id,
            "session_id": state.session_id,
        }) in ev.emitted

    async def test_idempotent_when_today_alive(self):
        plat, ev = FakePlatform(), FakeEvaluator()
        first = await ensure_today_session(_now_utc(), plat, CFG, evaluator=ev)
        second = await ensure_today_session(_now_utc(), plat, CFG, evaluator=ev)

        assert len(plat.spawns) == 1           # no second spawn
        assert second is not None and second.thread_id == first.thread_id
        assert plat.loaded == [first.thread_id]  # resumed, not rebuilt
        # Only one day_started emitted.
        assert sum(1 for n, _ in ev.emitted if n == "circadian:day_started") == 1

    async def test_rebuilds_when_thread_deleted(self):
        plat, ev = FakePlatform(), FakeEvaluator()
        first = await ensure_today_session(_now_utc(), plat, CFG, evaluator=ev)
        # Simulate genuine thread deletion (not a 24h TTL auto-archive, which
        # verify_thread_alive still treats as alive).
        plat.threads[first.thread_id] = False

        second = await ensure_today_session(_now_utc(), plat, CFG, evaluator=ev)
        assert len(plat.spawns) == 2
        assert second is not None and second.thread_id != first.thread_id
        # PR #476 review P1: the orphaned session must be finalized before the
        # rebuild, not left loaded until shutdown.
        assert first.thread_id in plat.closed

    async def test_no_channel_means_no_spawn(self):
        plat, ev = FakePlatform(channel_id=None), FakeEvaluator()
        state = await ensure_today_session(_now_utc(), plat, CFG, evaluator=ev)
        assert state is None
        assert plat.spawns == []
        assert CircadianState.load() is None

    async def test_spawn_failure_leaves_no_state(self):
        plat, ev = FakePlatform(), FakeEvaluator()
        plat.spawn_should_raise = True
        state = await ensure_today_session(_now_utc(), plat, CFG, evaluator=ev)
        assert state is None
        assert CircadianState.load() is None
        assert ev.emitted == []

    async def test_cross_day_recovers_then_spawns(self):
        # Seed a stale, unclosed yesterday state.
        stale = CircadianState(
            date=_yesterday(), thread_id=7, session_id="old",
            channel_id=999, started_at="2026-01-01T08:00:00+08:00",
        )
        with state_lock():
            stale.save_atomic()
        plat, ev = FakePlatform(), FakeEvaluator()
        plat.threads[7] = True

        state = await ensure_today_session(_now_utc(), plat, CFG, evaluator=ev)
        assert 7 in plat.closed                 # yesterday closed
        assert state is not None and state.date != _yesterday()
        assert len(plat.spawns) == 1            # today spawned
        names = [n for n, _ in ev.emitted]
        assert "circadian:day_closed" in names  # recovery emitted close
        assert "circadian:day_started" in names


# ===========================================================================
# close_today
# ===========================================================================


class TestCloseToday:
    async def test_close_stops_session_and_archives(self):
        plat, ev = FakePlatform(), FakeEvaluator()
        state = await ensure_today_session(_now_utc(), plat, CFG, evaluator=ev)
        ev.emitted.clear()

        await close_today(plat, CFG, evaluator=ev)
        assert plat.closed == [state.thread_id]
        # Live state archived away.
        assert CircadianState.load() is None
        archive = st.state_path().parent / "log" / f"{state.date}.json"
        assert archive.exists()
        assert any(n == "circadian:day_closed" for n, _ in ev.emitted)

    async def test_close_with_no_state_is_noop(self):
        plat, ev = FakePlatform(), FakeEvaluator()
        await close_today(plat, CFG, evaluator=ev)
        assert plat.closed == []
        assert ev.emitted == []


# ===========================================================================
# recover_on_startup
# ===========================================================================


class TestRecoverOnStartup:
    async def test_same_day_alive_resumes(self):
        plat, ev = FakePlatform(), FakeEvaluator()
        state = await ensure_today_session(_now_utc(), plat, CFG, evaluator=ev)
        plat.loaded.clear()

        await recover_on_startup(plat, CFG, evaluator=ev)
        assert plat.loaded == [state.thread_id]
        assert plat.closed == []                # not closed — it's today's

    async def test_cross_day_unclosed_force_closes(self):
        stale = CircadianState(
            date=_yesterday(), thread_id=7, session_id="old",
            channel_id=999, started_at="2026-01-01T08:00:00+08:00",
        )
        with state_lock():
            stale.save_atomic()
        plat, ev = FakePlatform(), FakeEvaluator()
        plat.threads[7] = True

        await recover_on_startup(plat, CFG, evaluator=ev)
        assert plat.closed == [7]
        assert CircadianState.load() is None    # archived
        assert plat.spawns == []                # recovery never spawns
        assert any(n == "circadian:day_closed" for n, _ in ev.emitted)

    async def test_cross_day_already_closed_archives_only(self):
        stale = CircadianState(
            date=_yesterday(), thread_id=7, session_id="old",
            channel_id=999, started_at="2026-01-01T08:00:00+08:00",
            closed_at="2026-01-01T23:59:00+08:00",
        )
        with state_lock():
            stale.save_atomic()
        plat, ev = FakePlatform(), FakeEvaluator()

        await recover_on_startup(plat, CFG, evaluator=ev)
        assert plat.closed == []                # already closed
        assert ev.emitted == []                 # no duplicate close event
        assert CircadianState.load() is None    # but still archived away

    async def test_no_state_is_noop(self):
        plat, ev = FakePlatform(), FakeEvaluator()
        await recover_on_startup(plat, CFG, evaluator=ev)
        assert plat.closed == [] and plat.spawns == []


# ===========================================================================
# Wiring — register_triggers + daemon direct-handler bypass
# ===========================================================================


def _make_daemon(session=None):
    from loom.autonomy.daemon import AutonomyDaemon
    from loom.notify.confirm import ConfirmFlow
    from loom.notify.router import NotificationRouter

    return AutonomyDaemon(
        notify_router=NotificationRouter(),
        confirm_flow=ConfirmFlow(send_fn=lambda n: None),
        loom_session=session,
    )


class TestWiring:
    def test_register_triggers_adds_dawn_and_close(self):
        daemon = _make_daemon()
        plat = FakePlatform()
        register_triggers(daemon, plat, CFG)

        names = {t.name for t in daemon.evaluator.list()}
        assert "circadian:dawn_spawn" in names
        assert "circadian:nightly_close" in names
        assert "circadian:dawn_spawn" in daemon._direct_handlers
        assert "circadian:nightly_close" in daemon._direct_handlers

    async def test_dawn_fire_bypasses_planner_and_spawns(self):
        daemon = _make_daemon()
        plat = FakePlatform()
        register_triggers(daemon, plat, CFG)

        # Planner must NOT be consulted for a direct-handler trigger.
        planner_called = False
        orig = daemon._planner.handle

        async def _spy(trigger, ctx):
            nonlocal planner_called
            planner_called = True
            return await orig(trigger, ctx)

        daemon._planner.handle = _spy

        dawn = next(t for t in daemon.evaluator.list() if t.name == "circadian:dawn_spawn")
        await daemon._on_trigger_fire(dawn, {"source": "cron"})

        assert planner_called is False
        assert len(plat.spawns) == 1

    async def test_close_fire_bypasses_planner_and_closes(self):
        daemon = _make_daemon()
        plat = FakePlatform()
        register_triggers(daemon, plat, CFG)
        # First spawn today via dawn handler.
        dawn = next(t for t in daemon.evaluator.list() if t.name == "circadian:dawn_spawn")
        await daemon._on_trigger_fire(dawn, {})
        assert len(plat.spawns) == 1

        close = next(t for t in daemon.evaluator.list() if t.name == "circadian:nightly_close")
        await daemon._on_trigger_fire(close, {})
        assert len(plat.closed) == 1
        assert CircadianState.load() is None

    async def test_setup_disabled_is_noop(self):
        daemon = _make_daemon()
        plat = FakePlatform()
        ok = await setup_circadian(daemon, plat, CircadianConfig(enabled=False))
        assert ok is False
        assert daemon.evaluator.list() == []

    async def test_setup_enabled_registers_and_catches_up(self):
        daemon = _make_daemon()
        plat = FakePlatform()
        ok = await setup_circadian(daemon, plat, CFG_ALWAYS)
        assert ok is True
        names = {t.name for t in daemon.evaluator.list()}
        assert {"circadian:dawn_spawn", "circadian:nightly_close"} <= names
        # CFG_ALWAYS is active at every minute → catch-up spawns today.
        assert len(plat.spawns) == 1


# ===========================================================================
# Pure helpers
# ===========================================================================


class TestHelpers:
    def test_active_hours_same_day_window(self):
        cfg = CircadianConfig(timezone=TZ, start="08:00", sleep="00:00")
        tz = ZoneInfo(TZ)
        assert is_in_active_hours(datetime(2026, 5, 27, 8, 0, tzinfo=tz), cfg)
        assert is_in_active_hours(datetime(2026, 5, 27, 23, 59, tzinfo=tz), cfg)
        assert not is_in_active_hours(datetime(2026, 5, 27, 3, 0, tzinfo=tz), cfg)
        assert not is_in_active_hours(datetime(2026, 5, 27, 7, 59, tzinfo=tz), cfg)

    def test_active_hours_wraps_midnight(self):
        # Night owl: 20:00 → 02:00 next day.
        cfg = CircadianConfig(timezone=TZ, start="20:00", sleep="02:00")
        tz = ZoneInfo(TZ)
        assert is_in_active_hours(datetime(2026, 5, 27, 23, 0, tzinfo=tz), cfg)
        assert is_in_active_hours(datetime(2026, 5, 27, 1, 0, tzinfo=tz), cfg)
        assert not is_in_active_hours(datetime(2026, 5, 27, 12, 0, tzinfo=tz), cfg)

    def test_utc_cron_conversion_taipei(self):
        # Asia/Taipei is UTC+8, no DST.
        assert local_hhmm_to_utc_cron("08:00", TZ) == "0 0 * * *"
        assert local_hhmm_to_utc_cron("00:00", TZ) == "0 16 * * *"
        assert local_hhmm_to_utc_cron("09:30", TZ) == "30 1 * * *"


# ===========================================================================
# Bot adapter — deterministic channel resolution (PR #476 review P2)
# ===========================================================================


class TestBotChannelResolution:
    def _stub_bot(self, *, channels, circadian=None):
        from loom.platform.discord.bot import LoomDiscordBot

        bot = LoomDiscordBot.__new__(LoomDiscordBot)
        bot._allowed_channels = set(channels)
        bot._channel_list = list(dict.fromkeys(channels))
        bot._circadian_channel_id = circadian
        return bot

    def test_first_allowed_channel_is_deterministic(self):
        # A set's iteration order is arbitrary; the ordered list is not.
        bot = self._stub_bot(channels=[111, 222, 333])
        assert bot.resolve_channel_id() == 111

    def test_explicit_circadian_channel_wins(self):
        bot = self._stub_bot(channels=[111, 222], circadian=999)
        assert bot.resolve_channel_id() == 999

    def test_none_when_no_channels(self):
        bot = self._stub_bot(channels=[])
        assert bot.resolve_channel_id() is None
