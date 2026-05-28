"""
Tests for CircadianState — the daily-session bookkeeping file (issue #459).

Covers the access discipline doc/57 §4 requires: atomic save/load roundtrip,
corruption + unknown-version quarantine, cross-day detection, archive+reset,
phase logging, and the non-blocking state lock used to arbitrate spawns.

All file IO is redirected away from the real ``~/.loom`` via
``set_dir_for_test`` so the suite never touches the user's home dir.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from loom.autonomy.circadian import state as st
from loom.autonomy.circadian.state import CircadianState, state_lock

TZ = "Asia/Taipei"


@pytest.fixture(autouse=True)
def _circ_dir(tmp_path):
    st.set_dir_for_test(tmp_path / "circadian")
    yield
    st.set_dir_for_test(None)


def _today() -> str:
    return datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d")


def _make(date: str | None = None) -> CircadianState:
    return CircadianState(
        date=date or _today(),
        thread_id=12345,
        session_id="daily-life-sess",
        channel_id=999,
        started_at=datetime.now(ZoneInfo(TZ)).isoformat(),
        timezone=TZ,
    )


class TestSaveLoad:
    def test_roundtrip(self):
        s = _make()
        with state_lock():
            s.save_atomic()
        loaded = CircadianState.load()
        assert loaded is not None
        assert loaded.thread_id == 12345
        assert loaded.session_id == "daily-life-sess"
        assert loaded.channel_id == 999
        assert loaded.version == 1
        assert loaded.closed_at is None

    def test_load_absent_returns_none(self):
        assert CircadianState.load() is None

    def test_save_leaves_no_temp_file(self):
        with state_lock():
            _make().save_atomic()
        leftovers = list((st.state_path().parent).glob(".state-*.tmp"))
        assert leftovers == []


class TestQuarantine:
    def test_corrupt_json_is_quarantined(self):
        sp = st.state_path()
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text("{ this is not json", encoding="utf-8")

        assert CircadianState.load() is None
        assert not sp.exists()
        broken = list(sp.parent.glob("state.json.broken-*"))
        assert len(broken) == 1

    def test_unknown_version_is_quarantined(self):
        sp = st.state_path()
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps({"version": 99, "date": _today()}), encoding="utf-8")

        assert CircadianState.load() is None
        assert list(sp.parent.glob("state.json.broken-*"))

    def test_missing_timezone_field_is_quarantined(self):
        """PR #479: ``timezone`` became required so the bot's freshness check
        has a tz to compare against. State written by pre-PR-#479 builds
        lacks the field and must be quarantined + rebuilt — silently using
        a fallback would reintroduce the stale-thread bug."""
        sp = st.state_path()
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps({
            "version": 1, "date": _today(), "thread_id": 1,
            "session_id": "x", "channel_id": 1,
            "started_at": "2026-01-01T08:00:00+08:00",
            # no timezone — pre-#479 schema
        }), encoding="utf-8")

        assert CircadianState.load() is None
        assert list(sp.parent.glob("state.json.broken-*"))


class TestCrossDay:
    def test_is_for_today_true(self):
        assert _make().is_for_today(TZ) is True

    def test_is_for_today_false_for_yesterday(self):
        yesterday = (datetime.now(ZoneInfo(TZ)) - timedelta(days=1)).strftime("%Y-%m-%d")
        assert _make(date=yesterday).is_for_today(TZ) is False


class TestArchive:
    def test_archive_and_reset(self):
        s = _make()
        with state_lock():
            s.save_atomic()
        s.archive_and_reset()

        # Live state gone, archived copy present under log/<date>.json.
        assert not st.state_path().exists()
        archive = st.state_path().parent / "log" / f"{s.date}.json"
        assert archive.exists()
        data = json.loads(archive.read_text(encoding="utf-8"))
        assert data["thread_id"] == 12345


class TestPhaseLog:
    def test_append_phase_records_outcome_and_reason(self):
        s = _make()
        s.append_phase("dawn", "spawned")
        s.append_phase("tick", "skipped", reason="no_state_transition")
        assert [e["phase"] for e in s.phase_log] == ["dawn", "tick"]
        assert s.phase_log[0]["outcome"] == "spawned"
        assert "reason" not in s.phase_log[0]
        assert s.phase_log[1]["reason"] == "no_state_transition"
        assert all("fired_at" in e for e in s.phase_log)

    def test_phase_log_survives_roundtrip(self):
        s = _make()
        s.append_phase("dawn", "spawned")
        with state_lock():
            s.save_atomic()
        loaded = CircadianState.load()
        assert loaded is not None
        assert loaded.phase_log[0]["phase"] == "dawn"


class TestStateLock:
    def test_nonblocking_miss_when_held(self):
        # Holding the lock (blocking) makes a non-blocking acquire from a
        # second fd in this process fail — the arbitration the spawn race
        # relies on (doc/57 §5, double-daemon row).
        with state_lock() as outer:
            assert outer is True
            with state_lock(blocking=False) as inner:
                assert inner is False
        # Released — a fresh non-blocking acquire now succeeds.
        with state_lock(blocking=False) as after:
            assert after is True
