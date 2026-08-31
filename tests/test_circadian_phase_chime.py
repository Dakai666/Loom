"""
Tests for rhythm-driven phase chime registration & delivery (issue #460).

Covers:
- ``register_rhythm_anchors`` registers one CronTrigger + direct handler
  per anchor, with closure correctly bound per anchor (no late-binding bug)
- a phase fire bypasses the planner, builds a ``circadian_today`` chime,
  routes through ``daemon.deliver_chime``, and logs the outcome to
  ``phase_log`` regardless of success
- ``setup_circadian`` integrates rhythm loading: with a table → anchors
  registered; without → only dawn/close, lifecycle unaffected
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from loom.autonomy.chime import ChimeRequest
from loom.autonomy.circadian import state as st
from loom.autonomy.circadian.lifecycle import (
    CircadianConfig,
    ensure_today_session,
    register_rhythm_anchors,
    register_triggers,
    reload_rhythm_anchors,
    setup_circadian,
)
from loom.autonomy.circadian.rhythm import Anchor
from loom.autonomy.circadian.state import CircadianState

TZ = "Asia/Taipei"
CFG = CircadianConfig(enabled=True, timezone=TZ, start="08:00", sleep="00:00")
CFG_ALWAYS = CircadianConfig(enabled=True, timezone=TZ, start="00:00", sleep="00:00")


@pytest.fixture(autouse=True)
def _circ_dir(tmp_path):
    st.set_dir_for_test(tmp_path / "circadian")
    yield
    st.set_dir_for_test(None)


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    """Run each test in its own workspace so the workspace-relative rhythm
    path (``autonomy/circadian/rhythm.toml``) is isolated per test."""
    monkeypatch.chdir(tmp_path)
    yield


class FakeEvaluator:
    def __init__(self):
        self.emitted: list[tuple[str, dict]] = []
        self.registered: list = []

    async def emit(self, name, payload):
        self.emitted.append((name, dict(payload)))

    def register(self, trigger):
        self.registered.append(trigger)


class FakePlatform:
    def __init__(self):
        self.threads: dict[int, bool] = {}
        self.loaded: list[int] = []
        self.closed: list[int] = []
        self.spawns: list[tuple[str, int]] = []
        self._next = 5000

    def resolve_channel_id(self):
        return 777

    async def spawn_daily_thread(self, *, name, channel_id):
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


def _make_daemon(deliveries=None):
    """Make a daemon whose chime callback records what was sent.

    ``deliveries`` is the list that will collect every ChimeRequest.
    Returns (daemon, deliveries, delivery_result_ref). Tests can flip
    ``delivery_result_ref[0]`` to simulate the bot accepting / rejecting.
    """
    from loom.autonomy.daemon import AutonomyDaemon
    from loom.notify.confirm import ConfirmFlow
    from loom.notify.router import NotificationRouter

    deliveries = [] if deliveries is None else deliveries
    delivery_result_ref = [True]

    async def _delivery(req: ChimeRequest) -> bool:
        deliveries.append(req)
        return delivery_result_ref[0]

    daemon = AutonomyDaemon(
        notify_router=NotificationRouter(),
        confirm_flow=ConfirmFlow(send_fn=lambda n: None),
        loom_session=None,
        chime_delivery=_delivery,
    )
    return daemon, deliveries, delivery_result_ref


def _write_rhythm(text: str) -> None:
    from pathlib import Path
    p = Path("autonomy/circadian/rhythm.toml")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class TestRegisterRhythmAnchors:
    def test_registers_one_trigger_per_anchor(self):
        daemon, _, _ = _make_daemon()
        anchors = [
            Anchor(time="08:00", name="dawn", meaning="x"),
            Anchor(time="09:00", name="shared_learning", meaning="y"),
            Anchor(time="23:00", name="evening_closure", meaning="z"),
        ]
        count = register_rhythm_anchors(daemon, CFG, anchors)
        assert count == 3
        names = {t.name for t in daemon.evaluator.list()}
        assert names == {
            "circadian:phase_dawn",
            "circadian:phase_shared_learning",
            "circadian:phase_evening_closure",
        }
        for n in names:
            assert n in daemon._direct_handlers

    def test_empty_anchors_is_noop(self):
        daemon, _, _ = _make_daemon()
        count = register_rhythm_anchors(daemon, CFG, [])
        assert count == 0
        assert daemon.evaluator.list() == []

    def test_multi_time_activity_registers_distinct_triggers(self):
        # Issue #526 end-to-end: a recurring activity (pet at 10:00 + 19:00)
        # from a real rhythm.toml must register *two* triggers + two handlers,
        # not silently collapse onto the first slot. The bug was a name
        # collision (both → ``circadian:phase_pet``); the @HHMM suffix fixes it.
        from loom.autonomy.circadian.rhythm import load_rhythm

        daemon, _, _ = _make_daemon()
        _write_rhythm('''
            [[anchors]]
            time = ["10:00", "19:00"]
            name = "pet"
            meaning = "喵吉照顧"
        ''')
        anchors = load_rhythm()
        count = register_rhythm_anchors(daemon, CFG, anchors)
        assert count == 2
        names = {t.name for t in daemon.evaluator.list()}
        assert names == {"circadian:phase_pet@1000", "circadian:phase_pet@1900"}
        for n in names:
            assert n in daemon._direct_handlers


class TestPhaseChimeFire:
    async def test_fire_routes_to_deliver_chime_with_circadian_today(self):
        daemon, deliveries, _ = _make_daemon()
        anchors = [
            Anchor(time="08:00", name="dawn", meaning="醒來成為今天的絲絲"),
        ]
        register_rhythm_anchors(daemon, CFG, anchors)

        # Spawn today so phase_log writes have somewhere to land.
        plat = FakePlatform()
        await ensure_today_session(
            datetime.now(timezone.utc), plat, CFG, evaluator=FakeEvaluator()
        )

        trigger = next(
            t for t in daemon.evaluator.list() if t.name == "circadian:phase_dawn"
        )
        await daemon._on_trigger_fire(trigger, {})

        assert len(deliveries) == 1
        req = deliveries[0]
        assert req.schedule_name == "circadian:phase_dawn"
        assert req.intent == "醒來成為今天的絲絲"
        assert req.target == {"type": "circadian_today", "fallback": "skip"}

    async def test_fire_forwards_anchor_permissions_to_chime(self):
        # Issue #525: the missing wire. _deliver_phase_chime must put the
        # anchor's allowed_tools / scope_grants onto the
        # ChimeRequest, exactly like the schedule path (daemon.py) does — so
        # bot._apply_chime_permissions can pre-authorise the phase's routine
        # tools. Without this the fields are declared but silently dropped.
        daemon, deliveries, _ = _make_daemon()
        anchors = [
            Anchor(
                time="11:00",
                name="curiosity",
                meaning="好奇心散步",
                allowed_tools=("fetch_url", "web_search"),
                scope_grants=(
                    {"resource": "path", "action": "write", "selector": "autonomy/circadian"},
                ),
            ),
        ]
        register_rhythm_anchors(daemon, CFG, anchors)

        plat = FakePlatform()
        await ensure_today_session(
            datetime.now(timezone.utc), plat, CFG, evaluator=FakeEvaluator()
        )

        trigger = next(
            t for t in daemon.evaluator.list() if t.name == "circadian:phase_curiosity"
        )
        await daemon._on_trigger_fire(trigger, {})

        assert len(deliveries) == 1
        req = deliveries[0]
        assert req.allowed_tools == ("fetch_url", "web_search")
        assert req.scope_grants == (
            {"resource": "path", "action": "write", "selector": "autonomy/circadian"},
        )
        # trust_level is not a phase field — never forwarded onto the chime.
        assert req.trust_level is None

    async def test_fire_without_permissions_leaves_chime_defaults(self):
        # A phase that declares no permission fields produces a ChimeRequest
        # with the neutral defaults — unchanged behaviour for existing tables.
        daemon, deliveries, _ = _make_daemon()
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="08:00", name="dawn", meaning="x"),
        ])
        plat = FakePlatform()
        await ensure_today_session(
            datetime.now(timezone.utc), plat, CFG, evaluator=FakeEvaluator()
        )
        trigger = next(
            t for t in daemon.evaluator.list() if t.name == "circadian:phase_dawn"
        )
        await daemon._on_trigger_fire(trigger, {})
        req = deliveries[0]
        assert req.trust_level is None
        assert req.allowed_tools == ()
        assert req.scope_grants == ()

    async def test_fire_bypasses_planner(self):
        daemon, _, _ = _make_daemon()
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="08:00", name="dawn", meaning="x"),
        ])
        planner_called = False
        orig = daemon._planner.handle

        async def _spy(trigger, ctx):
            nonlocal planner_called
            planner_called = True
            return await orig(trigger, ctx)

        daemon._planner.handle = _spy

        trigger = next(
            t for t in daemon.evaluator.list() if t.name == "circadian:phase_dawn"
        )
        await daemon._on_trigger_fire(trigger, {})
        assert planner_called is False

    async def test_each_handler_closes_over_its_own_anchor(self):
        """Regression: the closure must bind anchor per-iteration so different
        triggers route their own intent — not all collapse to the last one."""
        daemon, deliveries, _ = _make_daemon()
        anchors = [
            Anchor(time="08:00", name="dawn", meaning="intent-dawn"),
            Anchor(time="09:00", name="shared_learning", meaning="intent-learning"),
            Anchor(time="23:00", name="evening_closure", meaning="intent-evening"),
        ]
        register_rhythm_anchors(daemon, CFG, anchors)

        plat = FakePlatform()
        await ensure_today_session(
            datetime.now(timezone.utc), plat, CFG, evaluator=FakeEvaluator()
        )

        for anchor in anchors:
            trig = next(
                t for t in daemon.evaluator.list() if t.name == anchor.trigger_name
            )
            await daemon._on_trigger_fire(trig, {})

        intents = [r.intent for r in deliveries]
        assert intents == ["intent-dawn", "intent-learning", "intent-evening"]

    async def test_fire_logs_delivered_outcome_to_phase_log(self):
        daemon, _, _ = _make_daemon()
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="08:00", name="dawn", meaning="x"),
        ])

        plat = FakePlatform()
        await ensure_today_session(
            datetime.now(timezone.utc), plat, CFG, evaluator=FakeEvaluator()
        )
        # ensure_today_session writes a "dawn"/"spawned" entry; clear it so
        # the assertion below isolates the phase fire's own append.
        st_obj = CircadianState.load()
        from loom.autonomy.circadian.state import state_lock
        with state_lock():
            st_obj.phase_log.clear()
            st_obj.save_atomic()

        trig = next(
            t for t in daemon.evaluator.list() if t.name == "circadian:phase_dawn"
        )
        await daemon._on_trigger_fire(trig, {})

        reloaded = CircadianState.load()
        assert reloaded is not None
        assert reloaded.phase_log == [
            {"phase": "dawn", "fired_at": reloaded.phase_log[0]["fired_at"],
             "outcome": "delivered"},
        ]

    async def test_fire_logs_skipped_when_delivery_returns_false(self):
        daemon, _, delivery_ref = _make_daemon()
        delivery_ref[0] = False  # platform rejects
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="08:00", name="dawn", meaning="x"),
        ])

        plat = FakePlatform()
        await ensure_today_session(
            datetime.now(timezone.utc), plat, CFG, evaluator=FakeEvaluator()
        )
        from loom.autonomy.circadian.state import state_lock
        st_obj = CircadianState.load()
        with state_lock():
            st_obj.phase_log.clear()
            st_obj.save_atomic()

        trig = next(
            t for t in daemon.evaluator.list() if t.name == "circadian:phase_dawn"
        )
        await daemon._on_trigger_fire(trig, {})

        reloaded = CircadianState.load()
        assert reloaded.phase_log[-1]["outcome"] == "skipped"
        assert reloaded.phase_log[-1]["reason"] == "no_today_session"

    async def test_fire_when_no_state_does_not_crash(self):
        """A phase cron firing before any state exists (manual wipe, dev
        restart) must drop the phase_log append silently — not raise."""
        daemon, _, _ = _make_daemon()
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="08:00", name="dawn", meaning="x"),
        ])

        trig = next(
            t for t in daemon.evaluator.list() if t.name == "circadian:phase_dawn"
        )
        # No spawn, no state.json — must not raise.
        await daemon._on_trigger_fire(trig, {})
        assert CircadianState.load() is None


class TestSetupCircadianIntegration:
    async def test_setup_with_rhythm_table_registers_phase_anchors(self):
        _write_rhythm('''
            [[anchors]]
            time = "08:00"
            name = "dawn"
            meaning = "x"

            [[anchors]]
            time = "23:00"
            name = "evening_closure"
            meaning = "y"
        ''')
        daemon, _, _ = _make_daemon()
        plat = FakePlatform()
        ok = await setup_circadian(daemon, plat, CFG_ALWAYS)
        assert ok is True
        names = {t.name for t in daemon.evaluator.list()}
        assert {
            "circadian:dawn_spawn",
            "circadian:nightly_close",
            "circadian:phase_dawn",
            "circadian:phase_evening_closure",
        } <= names

    async def test_setup_without_rhythm_table_still_works(self):
        # No rhythm.toml exists in cwd → engine still registers dawn/close.
        daemon, _, _ = _make_daemon()
        plat = FakePlatform()
        ok = await setup_circadian(daemon, plat, CFG_ALWAYS)
        assert ok is True
        names = {t.name for t in daemon.evaluator.list()}
        assert {"circadian:dawn_spawn", "circadian:nightly_close"} <= names
        # Only the two lifecycle triggers — no phase anchors registered.
        phase_anchors = [n for n in names if n.startswith("circadian:phase_")]
        assert phase_anchors == []

    async def test_setup_with_malformed_rhythm_does_not_break_lifecycle(self):
        _write_rhythm("[[anchors\nname = 'dawn'")  # invalid TOML
        daemon, _, _ = _make_daemon()
        plat = FakePlatform()
        ok = await setup_circadian(daemon, plat, CFG_ALWAYS)
        assert ok is True
        # Lifecycle triggers still up; phase anchors empty.
        names = {t.name for t in daemon.evaluator.list()}
        assert "circadian:dawn_spawn" in names
        assert [n for n in names if n.startswith("circadian:phase_")] == []


def _write_weave(text: str) -> None:
    from pathlib import Path
    p = Path("autonomy/circadian/daily_weave.md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class TestWeaveCompositionInChime:
    """PR3 #461: daily_weave section composes into chime body after rhythm
    meaning, under a 「今日織程」 sub-heading so the agent can distinguish
    stable scaffolding from today-specific items."""

    async def test_weave_section_composes_after_meaning(self):
        _write_weave("""
## dawn
- recall 近期記憶
- 跟 DK 說早安
""")
        daemon, deliveries, _ = _make_daemon()
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="08:00", name="dawn", meaning="醒來成為今天的絲絲"),
        ])
        plat = FakePlatform()
        await ensure_today_session(
            datetime.now(timezone.utc), plat, CFG, evaluator=FakeEvaluator()
        )

        trig = next(
            t for t in daemon.evaluator.list() if t.name == "circadian:phase_dawn"
        )
        await daemon._on_trigger_fire(trig, {})

        intent = deliveries[0].intent
        assert "醒來成為今天的絲絲" in intent
        assert "**今日織程**" in intent
        assert "recall 近期記憶" in intent
        # Meaning comes first, weave second (ordering matters for the agent).
        assert intent.index("醒來成為今天的絲絲") < intent.index("**今日織程**")

    async def test_weave_missing_for_phase_falls_back_to_meaning(self):
        _write_weave("## shared_learning\n- 讀 HN top 10\n")  # no dawn section
        daemon, deliveries, _ = _make_daemon()
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="08:00", name="dawn", meaning="醒來"),
        ])
        plat = FakePlatform()
        await ensure_today_session(
            datetime.now(timezone.utc), plat, CFG, evaluator=FakeEvaluator()
        )
        trig = next(
            t for t in daemon.evaluator.list() if t.name == "circadian:phase_dawn"
        )
        await daemon._on_trigger_fire(trig, {})

        intent = deliveries[0].intent
        assert intent == "醒來"
        assert "今日織程" not in intent  # no spurious heading when section absent

    async def test_no_weave_file_preserves_pr2_behavior(self):
        """PR2 regression: with no weave file at all, intent is meaning only."""
        # No _write_weave call.
        daemon, deliveries, _ = _make_daemon()
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="08:00", name="dawn", meaning="只有 meaning"),
        ])
        plat = FakePlatform()
        await ensure_today_session(
            datetime.now(timezone.utc), plat, CFG, evaluator=FakeEvaluator()
        )
        trig = next(
            t for t in daemon.evaluator.list() if t.name == "circadian:phase_dawn"
        )
        await daemon._on_trigger_fire(trig, {})

        assert deliveries[0].intent == "只有 meaning"

    async def test_meaning_empty_with_weave_section_still_composes(self):
        """An anchor with empty meaning but a weave section — chime body is
        the weave section alone (not the generic fallback)."""
        _write_weave("## dawn\n- recall\n")
        daemon, deliveries, _ = _make_daemon()
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="08:00", name="dawn", meaning=""),
        ])
        plat = FakePlatform()
        await ensure_today_session(
            datetime.now(timezone.utc), plat, CFG, evaluator=FakeEvaluator()
        )
        trig = next(
            t for t in daemon.evaluator.list() if t.name == "circadian:phase_dawn"
        )
        await daemon._on_trigger_fire(trig, {})

        intent = deliveries[0].intent
        assert "**今日織程**" in intent
        assert "- recall" in intent
        assert "Circadian phase:" not in intent  # generic fallback NOT triggered


class TestDawnRevisionReport:
    """PR4 #462: dawn anchor surfaces yesterday's weave_revise result as
    a 4th chime body layer so DK gets informed without a confirm round-trip."""

    @staticmethod
    def _yesterday_str(tz="Asia/Taipei"):
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        return (datetime.now(ZoneInfo(tz)) - timedelta(days=1)).strftime("%Y-%m-%d")

    @staticmethod
    def _seed_proposal(subdir: str, rationale: str, changes_summary_token: str):
        from pathlib import Path

        from loom.autonomy.circadian.proposal import (
            APPLIED_SUBDIR,  # noqa: F401 — kept for explicit reference
            Change,
            PROPOSALS_DIR,
            WeaveProposal,
            _save_proposal_toml,
            proposal_path,
        )

        target_dir = PROPOSALS_DIR / subdir
        date = TestDawnRevisionReport._yesterday_str()
        p = WeaveProposal(
            date=date, phase="evening_closure", based_on_mtime=0,
            rationale=rationale,
            changes=[Change(
                section=changes_summary_token, action="add", new_body="- x",
            )],
        )
        _save_proposal_toml(p, proposal_path(date, target_dir))

    async def test_dawn_reports_applied_proposal(self):
        from loom.autonomy.circadian.proposal import APPLIED_SUBDIR
        self._seed_proposal(APPLIED_SUBDIR, "HN 沒看 → reading_block", "errand")

        daemon, deliveries, _ = _make_daemon()
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="08:00", name="dawn", meaning="醒來"),
        ])
        plat = FakePlatform()
        await ensure_today_session(
            datetime.now(timezone.utc), plat, CFG, evaluator=FakeEvaluator()
        )
        trig = next(
            t for t in daemon.evaluator.list() if t.name == "circadian:phase_dawn"
        )
        await daemon._on_trigger_fire(trig, {})

        intent = deliveries[0].intent
        assert "**昨夜你改了什麼**" in intent
        assert "errand" in intent
        assert "HN 沒看" in intent
        assert "簡述" in intent  # nudges agent to brief DK

    async def test_dawn_reports_conflict_proposal(self):
        from loom.autonomy.circadian.proposal import CONFLICTS_SUBDIR
        self._seed_proposal(CONFLICTS_SUBDIR, "想加 errand", "errand")

        daemon, deliveries, _ = _make_daemon()
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="08:00", name="dawn", meaning="醒來"),
        ])
        plat = FakePlatform()
        await ensure_today_session(
            datetime.now(timezone.utc), plat, CFG, evaluator=FakeEvaluator()
        )
        trig = next(
            t for t in daemon.evaluator.list() if t.name == "circadian:phase_dawn"
        )
        await daemon._on_trigger_fire(trig, {})

        intent = deliveries[0].intent
        assert "擋下" in intent
        assert "errand" in intent
        assert "問他要不要重 propose" in intent

    async def test_non_dawn_phase_does_not_get_revision_report(self):
        """Only dawn surfaces the revision report. shared_learning at 09:00
        should not show the yesterday-revision layer even if a proposal exists."""
        from loom.autonomy.circadian.proposal import APPLIED_SUBDIR
        self._seed_proposal(APPLIED_SUBDIR, "x", "y")

        daemon, deliveries, _ = _make_daemon()
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="09:00", name="shared_learning", meaning="共讀"),
        ])
        plat = FakePlatform()
        await ensure_today_session(
            datetime.now(timezone.utc), plat, CFG, evaluator=FakeEvaluator()
        )
        trig = next(
            t for t in daemon.evaluator.list()
            if t.name == "circadian:phase_shared_learning"
        )
        await daemon._on_trigger_fire(trig, {})

        intent = deliveries[0].intent
        assert "昨夜" not in intent

    async def test_dawn_no_proposal_no_report_layer(self):
        """Day without any overnight revision — dawn chime is just the
        usual two layers, no 「昨夜」 noise."""
        daemon, deliveries, _ = _make_daemon()
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="08:00", name="dawn", meaning="醒來"),
        ])
        plat = FakePlatform()
        await ensure_today_session(
            datetime.now(timezone.utc), plat, CFG, evaluator=FakeEvaluator()
        )
        trig = next(
            t for t in daemon.evaluator.list() if t.name == "circadian:phase_dawn"
        )
        await daemon._on_trigger_fire(trig, {})

        intent = deliveries[0].intent
        assert "昨夜" not in intent


class TestReloadRhythmAnchors:
    """Dawn-time reconciliation so rhythm.toml edits take effect next day
    without restarting the long-lived daemon (issue #477)."""

    def test_reload_registers_a_newly_added_anchor(self):
        _write_rhythm('''
            [[anchors]]
            time = "10:00"
            name = "pet"
            meaning = "喵吉"
        ''')
        daemon, _, _ = _make_daemon()
        reload_rhythm_anchors(daemon, CFG)
        # Author adds a second anchor and the day turns over.
        _write_rhythm('''
            [[anchors]]
            time = "10:00"
            name = "pet"
            meaning = "喵吉"

            [[anchors]]
            time = "14:00"
            name = "deep_weave"
            meaning = "深入"
        ''')
        count = reload_rhythm_anchors(daemon, CFG)
        assert count == 2
        names = {t.name for t in daemon.evaluator.list()}
        assert {"circadian:phase_pet", "circadian:phase_deep_weave"} <= names

    def test_reload_retires_a_removed_anchor(self):
        # The invariant that justified this issue: a removed anchor must stop
        # firing, not linger as an orphan trigger + dead handler.
        _write_rhythm('''
            [[anchors]]
            time = "10:00"
            name = "pet"
            meaning = "喵吉"

            [[anchors]]
            time = "11:00"
            name = "curiosity"
            meaning = "散步"
        ''')
        daemon, _, _ = _make_daemon()
        reload_rhythm_anchors(daemon, CFG)
        assert "circadian:phase_curiosity" in daemon._direct_handlers
        # Author drops the curiosity walk.
        _write_rhythm('''
            [[anchors]]
            time = "10:00"
            name = "pet"
            meaning = "喵吉"
        ''')
        reload_rhythm_anchors(daemon, CFG)
        names = {t.name for t in daemon.evaluator.list()}
        assert "circadian:phase_curiosity" not in names
        assert "circadian:phase_pet" in names
        # Handler torn down too — no dead entry left behind.
        assert "circadian:phase_curiosity" not in daemon._direct_handlers

    def test_reload_leaves_dawn_close_triggers_untouched(self):
        daemon, _, _ = _make_daemon()
        register_triggers(daemon, FakePlatform(), CFG)
        _write_rhythm('''
            [[anchors]]
            time = "10:00"
            name = "pet"
            meaning = "喵吉"
        ''')
        reload_rhythm_anchors(daemon, CFG)
        names = {t.name for t in daemon.evaluator.list()}
        assert {"circadian:dawn_spawn", "circadian:nightly_close"} <= names

    def test_reload_with_no_table_retires_all_anchors(self):
        _write_rhythm('''
            [[anchors]]
            time = "10:00"
            name = "pet"
            meaning = "喵吉"
        ''')
        daemon, _, _ = _make_daemon()
        reload_rhythm_anchors(daemon, CFG)
        # Table vanishes (deleted / unreadable) — tolerant contract: zero
        # anchors, every phase anchor retired, no crash.
        from pathlib import Path
        Path("autonomy/circadian/rhythm.toml").unlink()
        count = reload_rhythm_anchors(daemon, CFG)
        assert count == 0
        names = {t.name for t in daemon.evaluator.list()}
        assert [n for n in names if n.startswith("circadian:phase_")] == []

    async def test_dawn_fire_reloads_the_rhythm_table(self):
        # End-to-end: the dawn trigger's direct handler runs reload, so a table
        # edited while the daemon was up is live the moment the new day opens.
        daemon, _, _ = _make_daemon()
        register_triggers(daemon, FakePlatform(), CFG_ALWAYS)
        _write_rhythm('''
            [[anchors]]
            time = "10:00"
            name = "pet"
            meaning = "喵吉"
        ''')
        dawn = next(
            t for t in daemon.evaluator.list() if t.name == "circadian:dawn_spawn"
        )
        await daemon._on_trigger_fire(dawn, {})
        names = {t.name for t in daemon.evaluator.list()}
        assert "circadian:phase_pet" in names


class TestWeaveAppliedEmit:
    """Issue #472: dawn emits ``circadian:weave_applied`` when last night's
    weave_revise actually applied (the revised plan starts driving today).
    The shipped weave_revise proposes-and-applies atomically, so this single
    dawn-detected event is the honest 'weave changed, now in effect' signal —
    there is no separate evening 'proposed' event."""

    def _write_applied_proposal(self, *, rationale: str = "養生：晚睡改早睡"):
        """Persist yesterday's applied proposal artifact and return its date."""
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        from loom.autonomy.circadian.proposal import (
            APPLIED_SUBDIR,
            PROPOSALS_DIR,
            Change,
            WeaveProposal,
            _save_proposal_toml,
            proposal_path,
        )

        yesterday = (
            datetime.now(ZoneInfo(TZ)) - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        proposal = WeaveProposal(
            date=yesterday, phase="evening_closure", based_on_mtime=0,
            rationale=rationale,
            changes=[Change(section="check_in", action="replace", new_body="x")],
        )
        _save_proposal_toml(proposal, proposal_path(yesterday, PROPOSALS_DIR / APPLIED_SUBDIR))
        return yesterday

    async def _spy_emits(self, daemon):
        """Wrap the daemon's evaluator.emit to record (name, payload)."""
        emitted: list[tuple[str, dict]] = []
        orig = daemon.evaluator.emit

        async def _rec(name, payload):
            emitted.append((name, dict(payload)))
            return await orig(name, payload)

        daemon.evaluator.emit = _rec
        return emitted

    async def _fire(self, daemon, trigger_name):
        trig = next(t for t in daemon.evaluator.list() if t.name == trigger_name)
        await daemon._on_trigger_fire(trig, {})

    async def test_dawn_emits_weave_applied_when_proposal_applied(self):
        yesterday = self._write_applied_proposal(rationale="養生：晚睡改早睡")
        daemon, _, _ = _make_daemon()
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="08:00", name="dawn", meaning="醒來"),
        ])
        await ensure_today_session(
            datetime.now(timezone.utc), FakePlatform(), CFG, evaluator=FakeEvaluator()
        )
        emitted = await self._spy_emits(daemon)

        await self._fire(daemon, "circadian:phase_dawn")

        applied = [p for n, p in emitted if n == "circadian:weave_applied"]
        assert len(applied) == 1
        assert applied[0]["applied_from"] == yesterday
        assert applied[0]["rationale"] == "養生：晚睡改早睡"
        assert applied[0]["date"]  # today's stamp present

    async def test_dawn_no_emit_when_no_proposal(self):
        daemon, _, _ = _make_daemon()
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="08:00", name="dawn", meaning="醒來"),
        ])
        await ensure_today_session(
            datetime.now(timezone.utc), FakePlatform(), CFG, evaluator=FakeEvaluator()
        )
        emitted = await self._spy_emits(daemon)

        await self._fire(daemon, "circadian:phase_dawn")

        assert not [n for n, _ in emitted if n == "circadian:weave_applied"]

    async def test_non_dawn_phase_does_not_emit_weave_applied(self):
        # Even with an applied proposal present, only dawn emits — other
        # phases firing must not re-announce the weave change.
        self._write_applied_proposal()
        daemon, _, _ = _make_daemon()
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="11:00", name="curiosity", meaning="散步"),
        ])
        await ensure_today_session(
            datetime.now(timezone.utc), FakePlatform(), CFG, evaluator=FakeEvaluator()
        )
        emitted = await self._spy_emits(daemon)

        await self._fire(daemon, "circadian:phase_curiosity")

        assert not [n for n, _ in emitted if n == "circadian:weave_applied"]


class TestWeaveJoinDiagnostic:
    """Issue #565: when the weave file's H2 headings stop matching any
    anchor name, the 今日織程 layer vanishes from every chime. Absence is
    not a signal the agent can reason about — so the dawn chime says it
    outright, once a day, and the daemon log records it for DK."""

    async def test_dawn_chime_reports_dead_join(self):
        _write_weave("## 今日 Program\n- default\n\n## 今日重點\n- 喵吉\n")
        # The dawn layer reads the rhythm *table* (same source the anchor
        # registry is built from), so the file has to be on disk here.
        _write_rhythm(
            '[[anchors]]\ntime = "08:00"\nname = "dawn"\nmeaning = "醒來"\n\n'
            '[[anchors]]\ntime = "10:00"\nname = "pet"\nmeaning = "照顧喵吉"\n'
        )
        daemon, deliveries, _ = _make_daemon()
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="08:00", name="dawn", meaning="醒來"),
            Anchor(time="10:00", name="pet", meaning="照顧喵吉"),
        ])
        plat = FakePlatform()
        await ensure_today_session(
            datetime.now(timezone.utc), plat, CFG, evaluator=FakeEvaluator()
        )

        trig = next(
            t for t in daemon.evaluator.list() if t.name == "circadian:phase_dawn"
        )
        await daemon._on_trigger_fire(trig, {})

        intent = deliveries[0].intent
        assert "醒來" in intent          # meaning layer survives
        assert "今日 Program" in intent  # names the file's actual headings
        assert "dawn" in intent          # …and what they should have been

    async def test_non_dawn_chime_does_not_repeat_the_warning(self):
        """One report per day, at dawn. Repeating it at every phase would
        train the agent to skim past it."""
        _write_weave("## 今日 Program\n- default\n")
        daemon, deliveries, _ = _make_daemon()
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="08:00", name="dawn", meaning="醒來"),
            Anchor(time="10:00", name="pet", meaning="照顧喵吉"),
        ])
        plat = FakePlatform()
        await ensure_today_session(
            datetime.now(timezone.utc), plat, CFG, evaluator=FakeEvaluator()
        )

        trig = next(
            t for t in daemon.evaluator.list() if t.name == "circadian:phase_pet"
        )
        await daemon._on_trigger_fire(trig, {})

        intent = deliveries[0].intent
        assert "照顧喵吉" in intent
        assert "今日 Program" not in intent

    async def test_healthy_join_adds_nothing(self):
        _write_weave("## dawn\n- 早安\n")
        daemon, deliveries, _ = _make_daemon()
        register_rhythm_anchors(daemon, CFG, [
            Anchor(time="08:00", name="dawn", meaning="醒來"),
        ])
        plat = FakePlatform()
        await ensure_today_session(
            datetime.now(timezone.utc), plat, CFG, evaluator=FakeEvaluator()
        )

        trig = next(
            t for t in daemon.evaluator.list() if t.name == "circadian:phase_dawn"
        )
        await daemon._on_trigger_fire(trig, {})

        intent = deliveries[0].intent
        assert "**今日織程**" in intent
        assert "join key" not in intent

    async def test_registration_logs_the_dead_join(self, caplog):
        """DK reads logs, the agent reads chimes — both get told."""
        import logging
        _write_weave("## 今日 Program\n- default\n")
        daemon, _, _ = _make_daemon()
        with caplog.at_level(logging.WARNING, logger="loom.autonomy.circadian.lifecycle"):
            register_rhythm_anchors(daemon, CFG, [
                Anchor(time="08:00", name="dawn", meaning="醒來"),
            ])
        assert any("join key" in r.message or "對得上" in r.getMessage()
                   for r in caplog.records)
