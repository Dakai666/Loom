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
