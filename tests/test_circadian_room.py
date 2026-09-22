"""
Contract tests for the circadian room engine (virtual room × state machine).

Spec: outputs/doc/circadian_room/spec_v0.1.md (+ miji_signal_inventory.md §7).

The invariants that matter most — written first:
- furniture is the only source of signals; a signal is woken on at most once
  (dedupe by source + id + key), across ticks and restarts
- the wake matrix: urgent wakes unless sleeping; normal wakes only in
  ``free``; ambient never wakes on its own
- budget + min gap keep normal signals from flooding the thread; urgent
  bypasses both
- nothing is dropped silently: a signal that doesn't wake is queued, and
  the queue rides along with the next wake (merged into one chime)
- an undelivered wake (no daily session yet) stays queued, not lost
- a broken furniture (bad command, bad JSON, timeout) never breaks the tick
- the chime only describes the world — furniture text, verbatim
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from loom.autonomy.circadian import room
from loom.autonomy.circadian import state as st
from loom.autonomy.circadian.lifecycle import CircadianConfig
from loom.autonomy.circadian.room import (
    Furniture,
    RoomConfig,
    RoomState,
    Signal,
    decide,
    load_room,
    parse_furniture_output,
    room_tick,
)

TZ = "Asia/Taipei"
CIRC = CircadianConfig(enabled=True, timezone=TZ, start="08:00", sleep="00:00")
TPE = timezone(timedelta(hours=8))


def at(hh: int, mm: int = 0, day: int = 23) -> datetime:
    """A UTC instant for a Taipei wall-clock time."""
    return datetime(2026, 9, day, hh, mm, tzinfo=TPE).astimezone(timezone.utc)


@pytest.fixture(autouse=True)
def _circ_dir(tmp_path):
    st.set_dir_for_test(tmp_path / "circadian")
    yield
    st.set_dir_for_test(None)


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield


class FakeDaemon:
    def __init__(self, accept: bool = True):
        self.accept = accept
        self.chimes: list = []
        self.evaluator = self
        self.registered: list = []
        self.handlers: dict = {}

    async def deliver_chime(self, req):
        self.chimes.append(req)
        return self.accept

    def register(self, trigger):
        self.registered.append(trigger)

    def register_direct_handler(self, name, fn):
        self.handlers[name] = fn

    emitted: list

    async def emit(self, name, ctx):
        self.__dict__.setdefault("emitted", []).append((name, dict(ctx)))


MIJI = Furniture(
    name="miji",
    label="喵吉",
    command=("true",),
    priorities={"feel_sick": "urgent"},
    default_priority="normal",
    allowed_tools=("run_bash",),
)


def cfg(**kw) -> RoomConfig:
    base = dict(enabled=True, daily_budget=6, min_gap_minutes=45,
                queue_ttl_hours=12, furniture=(MIJI,))
    base.update(kw)
    return RoomConfig(**base)


def runner_returning(*batches):
    """A fake furniture runner yielding one (signals-json) batch per call."""
    calls = iter(batches)

    async def _run(furniture, now):
        try:
            raw = next(calls)
        except StopIteration:
            raw = {"signals": []}
        return parse_furniture_output(furniture, json.dumps(raw))

    return _run


def sig(id_: str, key: str, text: str = "…") -> dict:
    return {"id": id_, "key": key, "text": text}


# ---------------------------------------------------------------------------
# Wake matrix
# ---------------------------------------------------------------------------

class TestDecide:
    def _state(self, **kw) -> RoomState:
        return RoomState(date="2026-09-23", **kw)

    def test_urgent_wakes_when_awake_in_any_activity(self):
        for act in ("free", "focused", "tending"):
            assert decide("urgent", act, self._state(), cfg(), at(14)) == "wake"

    def test_urgent_queues_while_sleeping(self):
        assert decide("urgent", "sleeping", self._state(), cfg(), at(3)) == "queue"

    def test_normal_wakes_only_when_free(self):
        assert decide("normal", "free", self._state(), cfg(), at(14)) == "wake"
        assert decide("normal", "focused", self._state(), cfg(), at(14)) == "queue"
        assert decide("normal", "tending", self._state(), cfg(), at(14)) == "queue"
        assert decide("normal", "sleeping", self._state(), cfg(), at(3)) == "queue"

    def test_ambient_never_wakes(self):
        for act in ("free", "focused", "sleeping"):
            assert decide("ambient", act, self._state(), cfg(), at(14)) == "queue"

    def test_normal_respects_daily_budget(self):
        s = self._state(wakes_today=6)
        assert decide("normal", "free", s, cfg(daily_budget=6), at(14)) == "queue"

    def test_normal_respects_min_gap(self):
        s = self._state(last_wake_at=at(13, 30).isoformat())
        assert decide("normal", "free", s, cfg(min_gap_minutes=45), at(14)) == "queue"
        assert decide("normal", "free", s, cfg(min_gap_minutes=45), at(14, 16)) == "wake"

    def test_urgent_bypasses_budget_and_gap(self):
        s = self._state(wakes_today=99, last_wake_at=at(13, 59).isoformat())
        assert decide("urgent", "free", s, cfg(), at(14)) == "wake"


# ---------------------------------------------------------------------------
# Furniture output contract
# ---------------------------------------------------------------------------

class TestFurnitureOutput:
    def test_parses_signals_and_next_signal_at(self):
        raw = json.dumps({
            "signals": [sig("seeking", "2026-09-23:afternoon", "她用頭撞你的手")],
            "next_signal_at": "2026-09-23T09:00:00+00:00",
        })
        signals, nxt = parse_furniture_output(MIJI, raw)
        assert [s.id for s in signals] == ["seeking"]
        assert signals[0].source == "miji"
        assert signals[0].priority == "normal"
        assert signals[0].text == "她用頭撞你的手"
        assert nxt == datetime(2026, 9, 23, 9, tzinfo=timezone.utc)

    def test_priority_from_furniture_mapping(self):
        signals, _ = parse_furniture_output(
            MIJI, json.dumps({"signals": [sig("feel_sick", "k")]}))
        assert signals[0].priority == "urgent"

    def test_garbage_output_yields_nothing(self):
        assert parse_furniture_output(MIJI, "not json") == ([], None)
        assert parse_furniture_output(MIJI, json.dumps([1, 2])) == ([], None)

    def test_malformed_signal_is_dropped_individually(self):
        raw = json.dumps({"signals": [{"id": "no_key"}, sig("seeking", "k")]})
        signals, _ = parse_furniture_output(MIJI, raw)
        assert [s.id for s in signals] == ["seeking"]


# ---------------------------------------------------------------------------
# Tick: the end-to-end contract
# ---------------------------------------------------------------------------

class TestTick:
    async def test_normal_signal_in_free_wakes_with_verbatim_text(self):
        d = FakeDaemon()
        run = runner_returning({"signals": [sig("seeking", "2026-09-23:afternoon", "她坐上鍵盤。")]})
        await room_tick(d, CIRC, at(12), config=cfg(), runner=run)
        assert len(d.chimes) == 1
        req = d.chimes[0]
        assert req.schedule_name == "circadian:room@1200"
        assert req.target["type"] == "circadian_today"
        assert "她坐上鍵盤。" in req.intent
        assert "run_bash" in req.allowed_tools

    async def test_same_signal_never_wakes_twice(self):
        d = FakeDaemon()
        same = {"signals": [sig("seeking", "2026-09-23:afternoon")]}
        run = runner_returning(same, same)
        await room_tick(d, CIRC, at(12), config=cfg(), runner=run)
        await room_tick(d, CIRC, at(13), config=cfg(), runner=run)
        assert len(d.chimes) == 1

    async def test_dedupe_survives_restart(self):
        same = {"signals": [sig("seeking", "2026-09-23:afternoon")]}
        d1 = FakeDaemon()
        await room_tick(d1, CIRC, at(12), config=cfg(), runner=runner_returning(same))
        # fresh process: state comes from disk
        d2 = FakeDaemon()
        await room_tick(d2, CIRC, at(13), config=cfg(), runner=runner_returning(same))
        assert d2.chimes == []

    async def test_sleeping_signal_queues_then_wakes_once_awake(self):
        """A queued signal is re-decided every tick — it doesn't need another
        signal to carry it (review P0: queue must not mean 'demoted')."""
        d = FakeDaemon()
        run = runner_returning(
            {"signals": [sig("thunder", "2026-09-23:late_night", "床底傳來抗議聲。")]},
        )
        await room_tick(d, CIRC, at(2), config=cfg(), runner=run)
        assert d.chimes == []
        await room_tick(d, CIRC, at(8, 5), config=cfg(), runner=run)
        assert len(d.chimes) == 1
        assert "床底傳來抗議聲。" in d.chimes[0].intent
        assert RoomState.load().queue == []

    async def test_queued_urgent_is_not_lost_while_reemitted(self):
        """feel_sick re-emitted with the same key through the night must wake
        as soon as she's awake, not sit until TTL (review P0 repro)."""
        d = FakeDaemon()
        same = {"signals": [sig("feel_sick", "2026-09-23:dawn", "走路的樣子不對。")]}
        run = runner_returning(same, same, same)
        await room_tick(d, CIRC, at(5), config=cfg(), runner=run)
        await room_tick(d, CIRC, at(7), config=cfg(), runner=run)
        assert d.chimes == []
        await room_tick(d, CIRC, at(8), config=cfg(), runner=run)
        assert len(d.chimes) == 1

    async def test_undelivered_wake_is_retried(self):
        d = FakeDaemon(accept=False)
        run = runner_returning({"signals": [sig("feel_sick", "2026-09-23:morning")]})
        await room_tick(d, CIRC, at(10), config=cfg(), runner=run)
        d.accept = True
        await room_tick(d, CIRC, at(10, 5), config=cfg(), runner=run)
        assert len(d.chimes) == 2
        assert RoomState.load().queue == []

    async def test_gap_deferred_normal_wakes_after_gap(self):
        RoomState(date="2026-09-23", last_wake_at=at(11, 50).isoformat()).save()
        d = FakeDaemon()
        run = runner_returning({"signals": [sig("seeking", "2026-09-23:afternoon")]})
        await room_tick(d, CIRC, at(12), config=cfg(min_gap_minutes=45), runner=run)
        assert d.chimes == []
        await room_tick(d, CIRC, at(12, 40), config=cfg(min_gap_minutes=45), runner=run)
        assert len(d.chimes) == 1

    async def test_queued_ambient_never_wakes_by_itself(self):
        amb = Furniture(name="window", label="窗戶", command=("true",),
                        priorities={}, default_priority="ambient")
        d = FakeDaemon()
        run = runner_returning({"signals": [sig("dusk", "2026-09-23")]})
        for hh in (18, 19, 20):
            await room_tick(d, CIRC, at(hh), config=cfg(furniture=(amb,)), runner=run)
        assert d.chimes == []

    async def test_ambient_waits_in_queue(self):
        d = FakeDaemon()
        amb = Furniture(name="window", label="窗戶", command=("true",),
                        priorities={}, default_priority="ambient")
        run = runner_returning({"signals": [sig("dusk", "2026-09-23")]})
        await room_tick(d, CIRC, at(18), config=cfg(furniture=(amb,)), runner=run)
        assert d.chimes == []
        assert [q["id"] for q in RoomState.load().queue] == ["dusk"]

    async def test_undelivered_wake_stays_queued(self):
        d = FakeDaemon(accept=False)
        run = runner_returning({"signals": [sig("seeking", "2026-09-23:afternoon", "蹭。")]})
        await room_tick(d, CIRC, at(12), config=cfg(), runner=run)
        s = RoomState.load()
        assert s.wakes_today == 0
        assert [q["id"] for q in s.queue] == ["seeking"]

    async def test_several_signals_one_tick_make_one_chime(self):
        d = FakeDaemon()
        run = runner_returning({"signals": [
            sig("seeking", "2026-09-23:afternoon"),
            sig("feel_sick", "2026-09-23:afternoon"),
        ]})
        await room_tick(d, CIRC, at(12), config=cfg(), runner=run)
        assert len(d.chimes) == 1
        assert RoomState.load().wakes_today == 1

    async def test_budget_resets_on_a_new_day(self):
        RoomState(date="2026-09-22", wakes_today=6).save()
        d = FakeDaemon()
        run = runner_returning({"signals": [sig("seeking", "2026-09-23:afternoon")]})
        await room_tick(d, CIRC, at(12), config=cfg(daily_budget=6), runner=run)
        assert len(d.chimes) == 1

    async def test_stale_queue_entries_expire(self):
        RoomState(date="2026-09-23", queue=[{
            "source": "miji", "id": "thunder", "key": "old", "text": "舊的雷。",
            "priority": "normal", "at": at(0).isoformat(),
        }]).save()
        d = FakeDaemon()
        run = runner_returning({"signals": [sig("seeking", "2026-09-23:dusk", "新的。")]})
        await room_tick(d, CIRC, at(17), config=cfg(queue_ttl_hours=12), runner=run)
        assert "舊的雷。" not in d.chimes[0].intent

    async def test_next_signal_at_skips_furniture_until_due(self):
        calls = []

        async def run(furniture, now):
            calls.append(now)
            return [], at(17)

        d = FakeDaemon()
        await room_tick(d, CIRC, at(12, 5), config=cfg(), runner=run)
        await room_tick(d, CIRC, at(12, 10), config=cfg(), runner=run)
        await room_tick(d, CIRC, at(17), config=cfg(), runner=run)
        assert calls == [at(12, 5), at(17)]

    async def test_broken_furniture_does_not_break_tick(self):
        async def boom(furniture, now):
            raise RuntimeError("peek crashed")

        d = FakeDaemon()
        await room_tick(d, CIRC, at(12), config=cfg(), runner=boom)
        assert d.chimes == []

    async def test_disabled_room_does_nothing(self):
        d = FakeDaemon()
        run = runner_returning({"signals": [sig("seeking", "k")]})
        await room_tick(d, CIRC, at(12), config=cfg(enabled=False), runner=run)
        assert d.chimes == []

    async def test_every_decision_is_logged_raw(self):
        d = FakeDaemon()
        run = runner_returning({"signals": [sig("seeking", "2026-09-23:afternoon")]})
        await room_tick(d, CIRC, at(12), config=cfg(), runner=run)
        logs = list((st._log_dir()).glob("room-*.jsonl"))
        assert logs
        lines = [json.loads(l) for l in logs[0].read_text().splitlines()]
        assert any(l["event"] == "signal" and l["decision"] == "wake" for l in lines)
        assert any(l["event"] == "wake" and l["delivered"] for l in lines)


# ---------------------------------------------------------------------------
# Real subprocess runner
# ---------------------------------------------------------------------------

class TestRunner:
    async def test_runs_command_and_parses_stdout(self, tmp_path):
        script = tmp_path / "peek.py"
        script.write_text(
            "import json; print(json.dumps({'signals': "
            "[{'id': 'seeking', 'key': 'k', 'text': 'hi'}]}))"
        )
        import sys
        f = Furniture(name="miji", label="喵吉", command=(sys.executable, str(script)),
                      priorities={}, default_priority="normal")
        signals, _ = await room.run_furniture(f, at(12))
        assert [s.id for s in signals] == ["seeking"]

    async def test_failing_command_yields_nothing(self):
        f = Furniture(name="x", label="x", command=("false",),
                      priorities={}, default_priority="normal")
        assert await room.run_furniture(f, at(12)) == ([], None)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestLoadRoom:
    def test_missing_file_is_disabled(self):
        assert load_room(Path("nope.toml")).enabled is False

    def test_invalid_toml_is_disabled(self, tmp_path):
        p = tmp_path / "room.toml"
        p.write_text("[[[")
        assert load_room(p).enabled is False

    def test_loads_furniture_and_drops_bad_ones(self, tmp_path):
        p = tmp_path / "room.toml"
        p.write_text(
            '[room]\nenabled = true\ndaily_budget = 4\n\n'
            '[[furniture]]\nname = "miji"\nlabel = "喵吉"\n'
            'command = ["python3", "skills/pet-cat/pet.py", "peek"]\n'
            'default_priority = "normal"\nallowed_tools = ["run_bash"]\n'
            '[furniture.priorities]\nfeel_sick = "urgent"\n\n'
            '[[furniture]]\nname = "broken"\n'  # no command
            '\n[[furniture]]\nname = "bad_prio"\ncommand = ["x"]\n'
            'default_priority = "loud"\n'
        )
        c = load_room(p)
        assert c.enabled and c.daily_budget == 4
        assert [f.name for f in c.furniture] == ["miji"]
        assert c.furniture[0].priorities == {"feel_sick": "urgent"}
        assert c.furniture[0].command == ("python3", "skills/pet-cat/pet.py", "peek")


class TestRegister:
    def test_registers_tick_trigger_when_room_exists(self, tmp_path):
        p = Path("autonomy/circadian/room.toml")
        p.parent.mkdir(parents=True)
        p.write_text('[room]\nenabled = true\n[[furniture]]\nname = "m"\ncommand = ["x"]\n')
        d = FakeDaemon()
        assert room.register_room(d, CIRC) is True
        assert [t.name for t in d.registered] == ["circadian:room_tick"]
        assert "circadian:room_tick" in d.handlers

    def test_no_room_file_registers_nothing(self):
        d = FakeDaemon()
        assert room.register_room(d, CIRC) is False
        assert d.registered == []


class TestSetupWiring:
    async def test_setup_circadian_registers_room_tick(self, monkeypatch):
        from loom.autonomy.circadian import lifecycle

        p = Path("autonomy/circadian/room.toml")
        p.parent.mkdir(parents=True)
        p.write_text('[room]\nenabled = true\n[[furniture]]\nname = "m"\ncommand = ["x"]\n')

        async def _noop(*a, **k):
            return None

        monkeypatch.setattr(lifecycle, "recover_on_startup", _noop)
        monkeypatch.setattr(lifecycle, "is_in_active_hours", lambda *a: False)
        monkeypatch.setattr(lifecycle, "register_triggers", lambda *a: None)
        d = FakeDaemon()
        await lifecycle.setup_circadian(d, object(), CIRC)
        assert "circadian:room_tick" in d.handlers

    def test_example_room_toml_loads(self):
        repo = Path(__file__).resolve().parent.parent
        c = load_room(repo / "autonomy/circadian/room.example.toml")
        assert c.enabled
        assert c.furniture[0].priority_for("feel_sick") == "urgent"
        assert c.furniture[0].priority_for("seeking") == "normal"



# ---------------------------------------------------------------------------
# Review round 1 (subagent + opencode on PR #591)
# ---------------------------------------------------------------------------

def _circ_state(phases: list[tuple[str, str]], closed: bool = False):
    from loom.autonomy.circadian.state import CircadianState
    s = CircadianState(date="2026-09-23", thread_id=1, session_id="s", channel_id=1,
                       started_at=at(8).isoformat(), timezone=TZ,
                       closed_at=at(23).isoformat() if closed else None)
    for phase, outcome in phases:
        s.append_phase(phase, outcome)
    s.save_atomic()


def _rhythm_with_dawn():
    p = Path("autonomy/circadian/rhythm.toml")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[[anchors]]\ntime = "09:00"\nname = "dawn"\nmeaning = "醒來"\n')


class TestAwake:
    async def test_not_awake_before_dawn_phase_delivered(self):
        """Thread opens at 08:00 but she wakes at the dawn anchor; the room
        must not speak first."""
        _rhythm_with_dawn()
        _circ_state([("dawn", "spawned")])
        d = FakeDaemon()
        run = runner_returning({"signals": [sig("feel_sick", "2026-09-23:morning")]})
        await room_tick(d, CIRC, at(8, 30), config=cfg(), runner=run)
        assert d.chimes == []
        _circ_state([("dawn", "spawned"), ("dawn", "delivered")])
        await room_tick(d, CIRC, at(9, 5), config=cfg(), runner=run)
        assert len(d.chimes) == 1

    async def test_closed_session_counts_as_sleeping(self):
        _circ_state([("dawn", "delivered")], closed=True)
        d = FakeDaemon()
        run = runner_returning({"signals": [sig("feel_sick", "2026-09-23:night")]})
        await room_tick(d, CIRC, at(23, 30), config=cfg(), runner=run)
        assert d.chimes == []


class TestBudget:
    async def test_urgent_does_not_spend_normal_budget(self):
        d = FakeDaemon()
        run = runner_returning({"signals": [sig("feel_sick", "2026-09-23:afternoon")]})
        await room_tick(d, CIRC, at(12), config=cfg(), runner=run)
        assert len(d.chimes) == 1
        assert RoomState.load().wakes_today == 0


class TestEmit:
    async def test_every_new_signal_is_emitted(self):
        d = FakeDaemon()
        run = runner_returning({"signals": [sig("seeking", "2026-09-23:afternoon")]})
        await room_tick(d, CIRC, at(12), config=cfg(), runner=run)
        assert [n for n, _ in d.emitted] == ["circadian:room_signal"]
        ctx = d.emitted[0][1]
        assert ctx["source"] == "miji" and ctx["id"] == "seeking"
        assert ctx["decision"] == "wake"


class TestBounds:
    async def test_signals_per_peek_are_capped(self):
        d = FakeDaemon()
        many = {"signals": [sig(f"s{i}", "k") for i in range(200)]}
        await room_tick(d, CIRC, at(12), config=cfg(), runner=runner_returning(many))
        total = len(RoomState.load().queue) + d.chimes[0].intent.count("\n- ")
        assert total <= room.MAX_SIGNALS_PER_PEEK

    async def test_chime_lines_are_capped(self):
        RoomState(date="2026-09-23", queue=[
            {"source": "miji", "id": f"q{i}", "key": "k", "text": f"舊{i}",
             "priority": "ambient", "at": at(11).isoformat()} for i in range(50)
        ]).save()
        d = FakeDaemon()
        run = runner_returning({"signals": [sig("seeking", "2026-09-23:afternoon")]})
        await room_tick(d, CIRC, at(12), config=cfg(), runner=run)
        intent = d.chimes[0].intent
        assert intent.count("\n- ") <= room.MAX_CHIME_LINES + 1
        assert "還有" in intent

    async def test_far_future_next_signal_at_is_clamped(self):
        calls = []

        async def run(furniture, now):
            calls.append(now)
            return [], datetime(2999, 1, 1, tzinfo=timezone.utc)

        d = FakeDaemon()
        await room_tick(d, CIRC, at(12), config=cfg(), runner=run)
        later = at(12) + room.NEXT_CHECK_MAX + timedelta(minutes=5)
        await room_tick(d, CIRC, later, config=cfg(), runner=run)
        assert len(calls) == 2

    async def test_peek_is_logged_even_without_signals(self):
        async def run(furniture, now):
            return [], None

        await room_tick(FakeDaemon(), CIRC, at(12), config=cfg(), runner=run)
        lines = [json.loads(l) for l in next(st._log_dir().glob("room-*.jsonl")).read_text().splitlines()]
        assert any(l["event"] == "peek" and l["count"] == 0 for l in lines)

    async def test_oversized_output_yields_nothing(self, tmp_path):
        import sys
        script = tmp_path / "big.py"
        script.write_text(
            "import json; print(json.dumps({'signals': "
            "[{'id': 's', 'key': 'k', 'text': 'x' * (1024 * 1024)}]}))"
        )
        f = Furniture(name="big", label="big", command=(sys.executable, str(script)),
                      priorities={}, default_priority="normal")
        assert await room.run_furniture(f, at(12)) == ([], None)

    async def test_unparseable_queue_time_expires(self):
        RoomState(date="2026-09-23", queue=[
            {"source": "miji", "id": "x", "key": "k", "text": "壞時間", "priority": "normal", "at": "??"}
        ]).save()
        run = runner_returning({"signals": []})
        await room_tick(FakeDaemon(), CIRC, at(12), config=cfg(), runner=run)
        assert RoomState.load().queue == []


class TestSeen:
    async def test_reseen_key_is_kept_fresh(self, monkeypatch):
        monkeypatch.setattr(room, "SEEN_CAP", 3)
        d = FakeDaemon()
        amb = Furniture(name="m", label="m", command=("true",), priorities={},
                        default_priority="ambient")
        c = cfg(furniture=(amb,), queue_ttl_hours=1000)
        long_lived = sig("need", "live")
        batches = [{"signals": [long_lived, sig(f"n{i}", "k")]} for i in range(6)]
        run = runner_returning(*batches)
        for i in range(6):
            await room_tick(d, CIRC, at(12, i * 5), config=c, runner=run)
        ids = [q["id"] for q in RoomState.load().queue]
        assert ids.count("need") == 1


class TestTolerance:
    def test_wrong_typed_state_file_falls_back_per_field(self):
        p = room.room_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"date": "2026-09-23", "queue": "notalist",
                                 "seen": None, "next_check": [], "wakes_today": "x"}))
        s = RoomState.load()
        assert s.date == "2026-09-23"
        assert s.queue == [] and s.seen == [] and s.next_check == {} and s.wakes_today == 0

    @pytest.mark.parametrize("body", [
        "room = 5\n",
        "furniture = 3\n[room]\nenabled = true\n",
        '[room]\nenabled = "false"\n[[furniture]]\nname = "m"\ncommand = ["x"]\n',
    ])
    def test_bad_shapes_disable_instead_of_raising(self, tmp_path, body):
        p = tmp_path / "room.toml"
        p.write_text(body)
        assert load_room(p).enabled is False

    def test_string_allowed_tools_drops_furniture(self, tmp_path):
        p = tmp_path / "room.toml"
        p.write_text('[room]\nenabled = true\n[[furniture]]\nname = "m"\n'
                     'command = ["x"]\nallowed_tools = "run_bash"\n')
        assert load_room(p).furniture == ()

    def test_duplicate_furniture_name_keeps_first(self, tmp_path):
        p = tmp_path / "room.toml"
        p.write_text('[room]\nenabled = true\n'
                     '[[furniture]]\nname = "m"\ncommand = ["a"]\n'
                     '[[furniture]]\nname = "m"\ncommand = ["b"]\n')
        assert [f.command for f in load_room(p).furniture] == [("a",)]

    async def test_setup_survives_broken_room(self, monkeypatch):
        from loom.autonomy.circadian import lifecycle

        def boom(*a, **k):
            raise RuntimeError("room exploded")

        called = []

        async def recover(*a, **k):
            called.append("recover")

        monkeypatch.setattr(room, "register_room", boom)
        monkeypatch.setattr(lifecycle, "recover_on_startup", recover)
        monkeypatch.setattr(lifecycle, "is_in_active_hours", lambda *a: False)
        monkeypatch.setattr(lifecycle, "register_triggers", lambda *a: None)
        await lifecycle.setup_circadian(FakeDaemon(), object(), CIRC)
        assert called == ["recover"]
