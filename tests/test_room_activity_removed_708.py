"""#708 S4a item 3 — Remove unwired RoomState.activity shell while keeping legacy JSON load compatible."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from loom.autonomy.circadian import room
from loom.autonomy.circadian import state as st
from loom.autonomy.circadian.lifecycle import CircadianConfig
from loom.autonomy.circadian.room import (
    Furniture,
    RoomConfig,
    RoomState,
    decide,
    parse_furniture_output,
    room_tick,
)

TZ = "Asia/Taipei"
CIRC = CircadianConfig(enabled=True, timezone=TZ, start="08:00", sleep="00:00")
TPE = timezone(timedelta(hours=8))


def _at(hh: int, mm: int = 0, day: int = 23) -> datetime:
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


def test_room_state_has_no_activity_field() -> None:
    field_names = {f.name for f in dataclasses.fields(RoomState)}
    assert "activity" not in field_names
    state = RoomState(date="2026-09-23")
    assert not hasattr(state, "activity")
    assert "activity" not in dataclasses.asdict(state)


def test_room_module_has_no_unwired_activity_shell() -> None:
    assert not hasattr(room, "ACTIVITIES")
    assert not hasattr(room, "current_activity")


@pytest.mark.parametrize("legacy_activity", ["free", "focused", "tending", None])
def test_load_ignores_legacy_activity_key_gracefully(legacy_activity) -> None:
    p = room.room_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({
            "date": "2026-09-23",
            "activity": legacy_activity,
            "wakes_today": 2,
            "last_wake_at": _at(10).isoformat(),
            "queue": [{"source": "miji", "id": "s1", "key": "k1"}],
            "seen": ["miji:s0:k0"],
            "next_check": {"miji": _at(11).isoformat()},
            "weather": {},
            "prefer_local": True,
        }),
        encoding="utf-8",
    )

    loaded = RoomState.load()
    assert loaded.date == "2026-09-23"
    assert loaded.wakes_today == 2
    assert loaded.seen == ["miji:s0:k0"]
    assert loaded.prefer_local is True
    assert not hasattr(loaded, "activity")

    loaded.save()
    saved_raw = json.loads(p.read_text(encoding="utf-8"))
    assert "activity" not in saved_raw


def test_decide_uses_awake_bool_without_activity_matrix() -> None:
    cfg = RoomConfig(enabled=True, daily_budget=6, min_gap_minutes=45)
    state = RoomState(date="2026-09-23")
    assert decide("urgent", True, state, cfg, _at(14)) == "wake"
    assert decide("urgent", False, state, cfg, _at(3)) == "queue"
    assert decide("normal", True, state, cfg, _at(14)) == "wake"
    assert decide("normal", False, state, cfg, _at(3)) == "queue"
    assert decide("ambient", True, state, cfg, _at(14)) == "queue"
    assert decide("ambient", False, state, cfg, _at(3)) == "queue"


async def test_room_tick_handles_legacy_activity_state_json() -> None:
    p = room.room_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"date": "2026-09-23", "activity": "free", "wakes_today": 0}),
        encoding="utf-8",
    )

    class _FakeDaemon:
        def __init__(self):
            self.chimes = []
            self.evaluator = None

        async def deliver_chime(self, req):
            self.chimes.append(req)
            return True

    miji = Furniture(
        name="miji", label="喵吉", command=("true",),
        default_priority="normal", input_trust="local",
    )
    cfg = RoomConfig(enabled=True, furniture=(miji,))

    async def _runner(furniture, now):
        return parse_furniture_output(
            furniture,
            json.dumps({"signals": [{"id": "seeking", "key": "k1", "text": "蹭蹭。"}]}),
        )

    daemon = _FakeDaemon()
    await room_tick(daemon, CIRC, _at(12), config=cfg, runner=_runner)
    assert len(daemon.chimes) == 1
    assert "activity" not in json.loads(p.read_text(encoding="utf-8"))
