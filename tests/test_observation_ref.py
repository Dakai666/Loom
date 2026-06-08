"""
Prediction Spine — observation_ref contract (epic #528, issue #531; slice 3 前置).

``observation_ref`` is the auditable pointer that ties a reconciled prediction's
score to the exact runtime observation that produced it (I2). This module pins
its contract *before* the reconciliation pipeline (slice 3) consumes it:

* **Format** — ``"<source>:<row_id>"``; ``source`` must be on the whitelist.
* **I2 boundary** — the whitelist is *only* runtime observation tables
  (``action`` → action_records, ``session_log`` → session_log). A ref pointing
  anywhere else (e.g. ``semantic:``) is rejected: ground truth may never come
  from an LLM self-narrative store.
* **Reverse lookup** — ``resolve_observation_ref`` reads the ref back to a
  *normalized* observation (the dict shape resolvers consume). A ref to a
  missing row returns ``None`` (unreconcilable, **not** miscounted).
* **Terminal-state-hides-failure** — ``tool_success`` is derived by scanning
  the full ``state_history``, not the final_state column (a failed action is
  eventually MEMORIALIZED, masking the failure).
"""

import json

import pytest
import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.core.memory.observation import (
    OBSERVATION_SOURCES,
    make_observation_ref,
    parse_observation_ref,
    resolve_observation_ref,
)


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_observation.db")


@pytest_asyncio.fixture
async def store(tmp_db):
    s = SQLiteStore(tmp_db)
    await s.initialize()
    return s


@pytest_asyncio.fixture
async def db_conn(store):
    async with store.connect() as db:
        yield db


async def _insert_action(db, *, id, final_state, history_to_states, duration_ms=120.0):
    history = [{"from": "x", "to": s, "ts": "2026-06-08T00:00:00+00:00"} for s in history_to_states]
    await db.execute(
        "INSERT INTO action_records "
        "(id, envelope_id, session_id, turn_index, tool_name, call_id, "
        " final_state, duration_ms, state_history, created_at) "
        "VALUES (?, 'env', 'sess', 0, 'run_bash', 'c1', ?, ?, ?, ?)",
        (id, final_state, duration_ms, json.dumps(history), "2026-06-08T00:00:00+00:00"),
    )
    await db.commit()


async def _insert_session_log(db, *, content):
    cur = await db.execute(
        "INSERT INTO session_log (session_id, turn_index, role, content, created_at) "
        "VALUES ('sess', 0, 'tool', ?, '2026-06-08T00:00:00+00:00')",
        (content,),
    )
    await db.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Format + I2 whitelist boundary
# ---------------------------------------------------------------------------

class TestRefFormat:
    def test_whitelist_is_runtime_observation_tables(self):
        assert OBSERVATION_SOURCES == ("action", "session_log")

    def test_make_and_parse_roundtrip(self):
        ref = make_observation_ref("action", "abc-1")
        assert ref == "action:abc-1"
        assert parse_observation_ref(ref) == ("action", "abc-1")

    def test_make_rejects_non_whitelist_source(self):
        with pytest.raises(ValueError):
            make_observation_ref("semantic", "x")

    def test_make_rejects_empty_row_id(self):
        with pytest.raises(ValueError):
            make_observation_ref("action", "  ")

    def test_parse_rejects_malformed(self):
        with pytest.raises(ValueError):
            parse_observation_ref("no-colon-here")

    def test_parse_rejects_empty(self):
        with pytest.raises(ValueError):
            parse_observation_ref("")

    def test_parse_rejects_non_whitelist_prefix_I2(self):
        """I2: ground truth may not point at an LLM-narrative store."""
        with pytest.raises(ValueError):
            parse_observation_ref("semantic:rel:foo")
        with pytest.raises(ValueError):
            parse_observation_ref("episodic:123")


# ---------------------------------------------------------------------------
# Reverse lookup — action_records
# ---------------------------------------------------------------------------

class TestResolveAction:
    async def test_success_path_normalizes(self, db_conn):
        await _insert_action(
            db_conn, id="a-ok", final_state="memorialized",
            history_to_states=["authorized", "executing", "observed", "committed", "memorialized"],
            duration_ms=200.0,
        )
        obs = await resolve_observation_ref(db_conn, "action:a-ok")
        assert obs is not None
        assert obs["tool_success"] is True
        assert obs["final_state"] == "memorialized"
        assert obs["duration_ms"] == 200.0

    async def test_terminal_state_hides_failure(self, db_conn):
        """final_state='memorialized' but the action passed through 'aborted'."""
        await _insert_action(
            db_conn, id="a-fail", final_state="memorialized",
            history_to_states=["authorized", "executing", "aborted", "memorialized"],
        )
        obs = await resolve_observation_ref(db_conn, "action:a-fail")
        assert obs["tool_success"] is False  # scanned history, not just final_state

    async def test_missing_row_returns_none(self, db_conn):
        """Unreconcilable, not miscounted — a ref to a gone row is None."""
        assert await resolve_observation_ref(db_conn, "action:ghost") is None


# ---------------------------------------------------------------------------
# Reverse lookup — session_log
# ---------------------------------------------------------------------------

class TestResolveSessionLog:
    async def test_resolves_output(self, db_conn):
        rid = await _insert_session_log(db_conn, content="16 passed in 0.2s")
        obs = await resolve_observation_ref(db_conn, f"session_log:{rid}")
        assert obs is not None
        assert obs["output"] == "16 passed in 0.2s"

    async def test_missing_row_returns_none(self, db_conn):
        assert await resolve_observation_ref(db_conn, "session_log:999999") is None


# ---------------------------------------------------------------------------
# Drift guard — local failure-state mirror must match harness
# ---------------------------------------------------------------------------

class TestFailureStateDriftGuard:
    def test_local_mirror_matches_harness(self):
        from loom.core.harness.lifecycle import _FAILURE_STATES
        from loom.core.memory.observation import _FAILURE_STATE_VALUES
        assert _FAILURE_STATE_VALUES == {s.value for s in _FAILURE_STATES}
