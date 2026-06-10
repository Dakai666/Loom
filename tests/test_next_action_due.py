"""
Prediction Spine — ``next_action`` due-condition (epic #528, P0.5-a slice A, #537).

slice B's heartbeat bets *after* an action, so it has the ``call_id`` and uses
``after_action``. The explicit ``predict`` tool bets *before* the action runs —
there is no ``call_id`` yet. ``next_action`` closes that gap: a bet names the
*session*, the *tool*, and an *after* timestamp, and settles against the first
**terminal** ``action_records`` row for that (session, tool) created strictly
after the anchor. Still I2-clean — ground truth is an ``action_records`` row.

Contract pinned here (red first):

* **strictly-after** — only an action created *after* the anchor settles the
  bet; the predicting tool-call's own row (≤ anchor) never settles it.
* **earliest wins** — the *next* action, not just any later one.
* **session + tool scoped** — another session's, or another tool's, action does
  not settle the bet.
* **terminal-only** — a still-running action does not settle yet (``None`` →
  reconcile records it ``not_settled``, never a free score; pairs with I4).
* **I1 at the finder** — a ``next_action`` missing session/tool/after raises.
* **registry, not if/elif** — registered in ``DUE_CONDITION_KINDS`` so dispatch
  goes through ``find_settling_observation`` like every other kind.
"""

import json

import pytest
import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.core.memory.observation import (
    DUE_CONDITION_KINDS,
    find_settling_observation,
)

_TERMINAL_HISTORY = ["authorized", "executing", "observed", "committed", "memorialized"]


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_next_action.db")


@pytest_asyncio.fixture
async def store(tmp_db):
    s = SQLiteStore(tmp_db)
    await s.initialize()
    return s


@pytest_asyncio.fixture
async def db_conn(store):
    async with store.connect() as db:
        yield db


async def _insert_action(
    db, *, id, session="sess", tool="run_bash", final_state="memorialized",
    created_at, history=None, duration_ms=120.0,
):
    states = history if history is not None else _TERMINAL_HISTORY
    hist = [{"from": "x", "to": s, "ts": created_at} for s in states]
    await db.execute(
        "INSERT INTO action_records "
        "(id, envelope_id, session_id, turn_index, tool_name, call_id, "
        " final_state, duration_ms, state_history, created_at) "
        "VALUES (?, 'env', ?, 0, ?, ?, ?, ?, ?, ?)",
        (id, session, tool, f"call-{id}", final_state, duration_ms,
         json.dumps(hist), created_at),
    )
    await db.commit()


def _due(*, session="sess", tool="run_bash", after="2026-06-10T12:00:00+00:00"):
    return {"kind": "next_action", "session_id": session, "tool": tool, "after": after}


# ---------------------------------------------------------------------------
# Registry / dispatch
# ---------------------------------------------------------------------------

class TestRegistered:
    def test_next_action_in_dispatch_table(self):
        assert "next_action" in DUE_CONDITION_KINDS
        assert callable(DUE_CONDITION_KINDS["next_action"])

    async def test_dispatches_through_find_settling(self, db_conn):
        await _insert_action(db_conn, id="a1", created_at="2026-06-10T12:00:05+00:00")
        ref = await find_settling_observation(db_conn, _due())
        assert ref == "action:a1"


# ---------------------------------------------------------------------------
# Strictly-after + earliest-wins
# ---------------------------------------------------------------------------

class TestStrictlyAfter:
    async def test_action_before_anchor_does_not_settle(self, db_conn):
        """The predicting tool-call's own row (≤ anchor) must not settle the bet."""
        await _insert_action(db_conn, id="before", created_at="2026-06-10T11:59:59+00:00")
        assert await find_settling_observation(db_conn, _due()) is None

    async def test_action_at_anchor_does_not_settle(self, db_conn):
        await _insert_action(db_conn, id="at", created_at="2026-06-10T12:00:00+00:00")
        assert await find_settling_observation(db_conn, _due()) is None

    async def test_earliest_after_wins(self, db_conn):
        await _insert_action(db_conn, id="later", created_at="2026-06-10T12:05:00+00:00")
        await _insert_action(db_conn, id="next", created_at="2026-06-10T12:00:30+00:00")
        ref = await find_settling_observation(db_conn, _due())
        assert ref == "action:next"  # the *next* action, not just any later one


class TestUtcIsoStringOrdering:
    """絲絲 PR #540 P3: ``created_at > after`` is a SQLite *string* comparison,
    so it only equals time order while every timestamp is the same-zone, fixed-
    width ISO form. Pin that invariant — a future tz change (e.g. Asia/Taipei for
    display) leaking into stored created_at would silently break next_action.
    """

    def test_utc_iso_lexicographic_order_equals_chronological(self):
        from datetime import datetime, UTC, timedelta
        base = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
        # spans second/minute/hour rollovers + sub-second precision — the spots
        # naive string compare would trip if the field weren't zero-padded.
        dts = [
            base,
            base + timedelta(microseconds=1),
            base + timedelta(seconds=1),
            base + timedelta(seconds=9),
            base + timedelta(seconds=10),
            base + timedelta(minutes=1),
            base + timedelta(minutes=59, seconds=59),
            base + timedelta(hours=1),
        ]
        isos = [d.isoformat() for d in dts]
        assert sorted(isos) == isos                     # lexicographic == input
        assert isos == [d.isoformat() for d in sorted(dts)]  # == chronological

    async def test_finder_respects_anchor_across_second_rollover(self, db_conn):
        """A bona-fide finder check at the rollover boundary the string compare
        is most likely to fumble (…:09 vs …:10)."""
        await _insert_action(db_conn, id="t09", created_at="2026-06-10T12:00:09+00:00")
        await _insert_action(db_conn, id="t10", created_at="2026-06-10T12:00:10+00:00")
        # anchored just after :09 — only :10 should settle
        ref = await find_settling_observation(
            db_conn, _due(after="2026-06-10T12:00:09.500000+00:00"))
        assert ref == "action:t10"


# ---------------------------------------------------------------------------
# Session + tool scoping
# ---------------------------------------------------------------------------

class TestScoping:
    async def test_other_session_does_not_settle(self, db_conn):
        await _insert_action(
            db_conn, id="other", session="someone-else",
            created_at="2026-06-10T12:00:05+00:00",
        )
        assert await find_settling_observation(db_conn, _due()) is None

    async def test_other_tool_does_not_settle(self, db_conn):
        await _insert_action(
            db_conn, id="other-tool", tool="fetch_url",
            created_at="2026-06-10T12:00:05+00:00",
        )
        assert await find_settling_observation(db_conn, _due(tool="run_bash")) is None

    async def test_matches_named_tool_only(self, db_conn):
        await _insert_action(db_conn, id="noise", tool="fetch_url",
                             created_at="2026-06-10T12:00:03+00:00")
        await _insert_action(db_conn, id="target", tool="run_bash",
                             created_at="2026-06-10T12:00:05+00:00")
        ref = await find_settling_observation(db_conn, _due(tool="run_bash"))
        assert ref == "action:target"


# ---------------------------------------------------------------------------
# Terminal-only
# ---------------------------------------------------------------------------

class TestTerminalOnly:
    async def test_running_action_does_not_settle_yet(self, db_conn):
        """A non-terminal action is not settled — None, not a free score (I4)."""
        await _insert_action(
            db_conn, id="running", final_state="executing",
            history=["authorized", "executing"],
            created_at="2026-06-10T12:00:05+00:00",
        )
        assert await find_settling_observation(db_conn, _due()) is None

    async def test_failed_terminal_action_settles(self, db_conn):
        """A run-and-failed action (aborted→memorialized) IS settled — it's the
        miss we want to score, not skip."""
        await _insert_action(
            db_conn, id="failed", final_state="memorialized",
            history=["authorized", "executing", "aborted", "memorialized"],
            created_at="2026-06-10T12:00:05+00:00",
        )
        assert await find_settling_observation(db_conn, _due()) == "action:failed"


# ---------------------------------------------------------------------------
# I1 at the finder
# ---------------------------------------------------------------------------

class TestRequiredFields:
    async def test_missing_session_raises(self, db_conn):
        with pytest.raises(ValueError):
            await find_settling_observation(
                db_conn, {"kind": "next_action", "tool": "run_bash", "after": "2026-06-10T12:00:00+00:00"},
            )

    async def test_missing_tool_raises(self, db_conn):
        with pytest.raises(ValueError):
            await find_settling_observation(
                db_conn, {"kind": "next_action", "session_id": "sess", "after": "2026-06-10T12:00:00+00:00"},
            )

    async def test_missing_after_raises(self, db_conn):
        with pytest.raises(ValueError):
            await find_settling_observation(
                db_conn, {"kind": "next_action", "session_id": "sess", "tool": "run_bash"},
            )
