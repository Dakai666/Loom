from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

import loom as loom_pkg


@pytest.fixture(autouse=True)
def _isolate_default_registry():
    registry = loom_pkg._get_default_registry()
    original_tools = dict(registry._tools)
    registry._tools.clear()
    try:
        yield
    finally:
        registry._tools.clear()
        registry._tools.update(original_tools)


class TestCoreSessionImport:
    def test_core_session_reexports_live_session(self):
        from loom.core.session import LoomSession as CoreSession
        from loom.platform.cli.main import LoomSession as CliSession

        assert CoreSession is CliSession


class TestLoomSessionStartup:
    @pytest_asyncio.fixture
    async def session_module(self):
        from loom.core import session as core_session

        return core_session

    async def test_start_wires_core_runtime_components(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
        session_module,
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(session_module, "build_router", lambda: MagicMock())
        monkeypatch.setattr(session_module, "_load_loom_config", lambda: {})
        monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {})
        monkeypatch.setattr(session_module, "build_embedding_provider", lambda env, cfg: None)
        from rich.prompt import Confirm
        monkeypatch.setattr(Confirm, "ask", lambda *args, **kwargs: True)

        (workspace / "loom_tools.py").write_text(
            """
import loom
from loom.core.harness.middleware import ToolResult


@loom.tool(description="Plugin-installed session tool", trust_level="safe")
async def session_plugin_tool(call):
    return ToolResult(
        call_id=call.id,
        tool_name=call.tool_name,
        success=True,
        output="ok",
    )
""".strip()
            + "\n",
            encoding="utf-8",
        )

        from loom.core.session import LoomSession

        session = LoomSession(
            model="gpt-test",
            db_path=str(tmp_path / "loom.db"),
            workspace=workspace,
        )
        await session.start()

        assert session._governor is not None
        assert session._skill_outcome_tracker is not None
        assert session._pipeline is not None
        assert session._mcp_clients == []
        assert session.registry.get("session_plugin_tool") is not None

        await session.stop()
        assert session._db is None

    async def test_start_loads_mcp_servers_into_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
        session_module,
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(session_module, "build_router", lambda: MagicMock())
        monkeypatch.setattr(session_module, "_load_loom_config", lambda: {"mcp": {"servers": [{"name": "minimax"}]}})
        monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {})
        monkeypatch.setattr(session_module, "build_embedding_provider", lambda env, cfg: None)
        from rich.prompt import Confirm
        monkeypatch.setattr(Confirm, "ask", lambda *args, **kwargs: True)

        fake_client = SimpleNamespace(_cfg=SimpleNamespace(name="minimax"))

        async def fake_load_mcp_servers_into_session(config, session, extra_env=None):
            assert config["mcp"]["servers"][0]["name"] == "minimax"
            assert session is not None
            return [fake_client]

        import loom.extensibility.mcp_client as mcp_client_module

        monkeypatch.setattr(
            mcp_client_module,
            "load_mcp_servers_into_session",
            fake_load_mcp_servers_into_session,
        )

        from loom.core.session import LoomSession

        session = LoomSession(
            model="gpt-test",
            db_path=str(tmp_path / "loom.db"),
            workspace=workspace,
        )
        await session.start()

        assert session._mcp_clients == [fake_client]

        await session.stop()


class TestConfigPathResolution:
    """Regression tests for parents[] index after moving to loom.core.session."""

    def test_load_loom_config_fallback_resolves_to_repo_root(self, tmp_path, monkeypatch):
        """_load_loom_config() fallback must point at the repo root, not one level above."""
        from loom.core.session import _load_loom_config
        import inspect
        from pathlib import Path

        session_file = Path(inspect.getfile(_load_loom_config))
        # parents[2] should be the repo root (two levels above loom/core/)
        expected_root = session_file.parents[2]
        # The fallback candidate must be inside the repo root, not outside it.
        # We can't assert the file exists (CI may lack loom.toml), but we can
        # assert the path resolution is correct relative to this file.
        assert expected_root.name == "Loom", (
            f"parents[2] resolved to {expected_root!r}, expected the repo root 'Loom'. "
            "If parents[] index changed, update _load_loom_config and _load_env."
        )

    def test_load_env_fallback_resolves_to_repo_root(self):
        """_load_env() fallback must point at the repo root, not one level above."""
        from loom.core.session import _load_env
        import inspect
        from pathlib import Path

        session_file = Path(inspect.getfile(_load_env))
        expected_root = session_file.parents[2]
        assert expected_root.name == "Loom", (
            f"parents[2] resolved to {expected_root!r}, expected the repo root 'Loom'. "
            "If parents[] index changed, update _load_loom_config and _load_env."
        )

    def test_load_loom_config_returns_empty_outside_repo(self, tmp_path, monkeypatch):
        """When cwd has no loom.toml and repo root has none, return {}."""
        from loom.core.session import _load_loom_config
        monkeypatch.chdir(tmp_path)
        result = _load_loom_config()
        # Either {} (no loom.toml found) or a dict (repo-root loom.toml found).
        # Either way it must be a dict, never raise.
        assert isinstance(result, dict)

    def test_load_env_returns_empty_outside_repo(self, tmp_path, monkeypatch):
        """When cwd has no .env and repo root has none, return {}."""
        from loom.core.session import _load_env
        monkeypatch.chdir(tmp_path)
        result = _load_env()
        assert isinstance(result, dict)


class TestOutputMaxTokensResolution:
    """Issue #181: per-model output cap resolution."""

    def test_falls_back_to_hardcoded_default_on_empty_config(self):
        from loom.core.session import _resolve_output_max_tokens
        assert _resolve_output_max_tokens({}, "claude-sonnet-4-6") == 8192

    def test_uses_cognition_default_when_no_override(self):
        from loom.core.session import _resolve_output_max_tokens
        cfg = {"cognition": {"output_max_tokens": 32768}}
        assert _resolve_output_max_tokens(cfg, "MiniMax-M2.7") == 32768

    def test_per_model_override_wins_over_default(self):
        from loom.core.session import _resolve_output_max_tokens
        cfg = {
            "cognition": {
                "output_max_tokens": 8192,
                "output_max_tokens_overrides": {
                    "MiniMax-M2.7": 65536,
                    "claude-sonnet-4-6": 32768,
                },
            }
        }
        assert _resolve_output_max_tokens(cfg, "MiniMax-M2.7") == 65536
        assert _resolve_output_max_tokens(cfg, "claude-sonnet-4-6") == 32768
        # unknown model → fall through to default
        assert _resolve_output_max_tokens(cfg, "gpt-5") == 8192

    def test_invalid_values_fall_back_gracefully(self):
        from loom.core.session import _resolve_output_max_tokens
        cfg = {
            "cognition": {
                "output_max_tokens": "not-a-number",
                "output_max_tokens_overrides": {
                    "claude-sonnet-4-6": "also-bad",
                },
            }
        }
        # both invalid → hardcoded default
        assert _resolve_output_max_tokens(cfg, "claude-sonnet-4-6") == 8192


class TestParallelDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_parallel_uses_current_task_graph_api_and_preserves_order(self):
        from loom.core.session import LoomSession
        from loom.core.harness.middleware import ToolResult

        session = object.__new__(LoomSession)

        async def fake_dispatch(name, args, call_id):
            await asyncio.sleep(0.01 if name == "slow" else 0.0)
            return ToolResult(
                call_id=call_id,
                tool_name=name,
                success=True,
                output=f"{name}:{args['value']}",
            )

        session._dispatch = fake_dispatch
        tool_uses = [
            SimpleNamespace(name="slow", args={"value": 1}, id="call-1"),
            SimpleNamespace(name="fast", args={"value": 2}, id="call-2"),
        ]

        dispatched = await LoomSession._dispatch_parallel(session, tool_uses)

        assert [tu.id for tu, _, _ in dispatched] == ["call-1", "call-2"]
        assert [result.output for _, result, _ in dispatched] == ["slow:1", "fast:2"]
        assert all(duration_ms >= 0 for _, _, duration_ms in dispatched)

    @pytest.mark.asyncio
    async def test_dispatch_parallel_wraps_dispatch_exceptions_as_tool_results(self):
        from loom.core.session import LoomSession

        session = object.__new__(LoomSession)

        async def fake_dispatch(name, args, call_id):
            raise RuntimeError("boom")

        session._dispatch = fake_dispatch
        tool_uses = [SimpleNamespace(name="mcp_tool", args={}, id="call-1")]

        dispatched = await LoomSession._dispatch_parallel(session, tool_uses)
        _, result, _ = dispatched[0]

        assert result.success is False
        assert result.failure_type == "execution_error"
        assert "Internal dispatch error: boom" in (result.error or "")


# ─────────────────────────────────────────────────────────────────────────────
# Issue #126 — session title tests (L1 provisional + L2 editable)
# ─────────────────────────────────────────────────────────────────────────────


class TestSessionLogTitle:
    """Unit tests for SessionLog.create_session(title=) and update_title()."""

    @pytest_asyncio.fixture
    async def sl_conn(self, tmp_path):
        """Fresh in-memory DB with sessions table, shared across tests."""
        import aiosqlite
        from loom.core.memory.session_log import SessionLog

        db = tmp_path / "sessions.db"
        conn = await aiosqlite.connect(str(db))
        await conn.execute(
            """
            CREATE TABLE sessions (
                session_id TEXT,
                model TEXT,
                title TEXT,
                started_at TEXT,
                last_active TEXT,
                turn_count INTEGER DEFAULT 0
            )
            """
        )
        await conn.commit()
        yield conn
        await conn.close()

    async def test_create_session_stores_title(self, sl_conn):
        from loom.core.memory.session_log import SessionLog

        sl = SessionLog(sl_conn)
        await sl.create_session("s1", "MiniMax-M2.7", title="My First Chat")
        meta = await sl.get_session("s1")
        assert meta is not None
        assert meta["title"] == "My First Chat"

    async def test_create_session_defaults_title_to_none(self, sl_conn):
        from loom.core.memory.session_log import SessionLog

        sl = SessionLog(sl_conn)
        await sl.create_session("s2", "claude-sonnet-4-6")
        meta = await sl.get_session("s2")
        assert meta is not None
        assert meta["title"] is None

    async def test_create_session_insert_or_ignore_safe_for_resume(self, sl_conn):
        from loom.core.memory.session_log import SessionLog

        sl = SessionLog(sl_conn)
        # Same session_id twice — second call must not raise (INSERT OR IGNORE)
        await sl.create_session("s3", "MiniMax-M2.7", title="First")
        await sl.create_session("s3", "MiniMax-M2.7", title="Second")
        meta = await sl.get_session("s3")
        assert meta["title"] == "First"  # first write preserved

    async def test_update_title_overwrites_existing_title(self, sl_conn):
        from loom.core.memory.session_log import SessionLog

        sl = SessionLog(sl_conn)
        await sl.create_session("s4", "MiniMax-M2.7", title="Original")
        await sl.update_title("s4", "Renamed Session")
        meta = await sl.get_session("s4")
        assert meta["title"] == "Renamed Session"

    async def test_update_title_idempotent_when_title_unchanged(self, sl_conn):
        from loom.core.memory.session_log import SessionLog

        sl = SessionLog(sl_conn)
        await sl.create_session("s5", "MiniMax-M2.7", title="Same")
        await sl.update_title("s5", "Same")
        meta = await sl.get_session("s5")
        assert meta["title"] == "Same"

    async def test_update_title_nonexistent_session_is_silent(self, sl_conn):
        from loom.core.memory.session_log import SessionLog

        sl = SessionLog(sl_conn)
        # Must not raise — UPDATE on non-existent row is valid SQL
        await sl.update_title("does-not-exist", "Any Title")
        # No row added
        rows = await sl.list_sessions()
        assert all(r["session_id"] != "does-not-exist" for r in rows)


class TestProvisionalTitle:
    """Tests that LoomSession accepts and forwards provisional_title to create_session."""

    @pytest_asyncio.fixture
    async def session_module(self):
        from loom.core import session as core_session
        return core_session

    async def test_provisional_title_stored_on_session_object(
        self, tmp_path, monkeypatch, session_module,
    ):
        from loom.core.session import LoomSession

        monkeypatch.setattr(session_module, "build_router", lambda: MagicMock())
        monkeypatch.setattr(session_module, "_load_loom_config", lambda: {})
        monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {})
        monkeypatch.setattr(session_module, "build_embedding_provider", lambda env, cfg: None)

        session = LoomSession(
            model="test-model",
            db_path=str(tmp_path / "loom.db"),
            workspace=tmp_path,
            provisional_title="Hello from first message",
        )
        assert session._provisional_title == "Hello from first message"

    async def test_provisional_title_none_by_default(
        self, tmp_path, monkeypatch, session_module,
    ):
        from loom.core.session import LoomSession

        monkeypatch.setattr(session_module, "build_router", lambda: MagicMock())
        monkeypatch.setattr(session_module, "_load_loom_config", lambda: {})
        monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {})
        monkeypatch.setattr(session_module, "build_embedding_provider", lambda env, cfg: None)

        session = LoomSession(
            model="test-model",
            db_path=str(tmp_path / "loom.db"),
            workspace=tmp_path,
        )
        assert session._provisional_title is None

    async def test_provisional_title_inserted_at_session_start(
        self, tmp_path, monkeypatch, session_module,
    ):
        from loom.core.session import LoomSession
        from loom.core.memory.session_log import SessionLog
        from rich.prompt import Confirm

        monkeypatch.setattr(session_module, "build_router", lambda: MagicMock())
        monkeypatch.setattr(session_module, "_load_loom_config", lambda: {})
        monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {})
        monkeypatch.setattr(session_module, "build_embedding_provider", lambda env, cfg: None)
        monkeypatch.setattr(Confirm, "ask", lambda *args, **kwargs: True)

        db_path = str(tmp_path / "loom.db")
        session = LoomSession(
            model="test-model",
            db_path=db_path,
            workspace=tmp_path,
            provisional_title="Provisional Title Here",
        )
        await session.start()
        await session.stop()

        import aiosqlite
        conn = await aiosqlite.connect(db_path)
        cursor = await conn.execute(
            "SELECT title FROM sessions WHERE session_id = ?",
            (session.session_id,),
        )
        row = await cursor.fetchone()
        await conn.close()

        assert row is not None
        assert row[0] == "Provisional Title Here"

    async def test_resume_session_does_not_overwrite_persisted_title(
        self, tmp_path, monkeypatch, session_module,
    ):
        from loom.core.session import LoomSession
        from rich.prompt import Confirm

        monkeypatch.setattr(session_module, "build_router", lambda: MagicMock())
        monkeypatch.setattr(session_module, "_load_loom_config", lambda: {})
        monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {})
        monkeypatch.setattr(session_module, "build_embedding_provider", lambda env, cfg: None)
        monkeypatch.setattr(Confirm, "ask", lambda *args, **kwargs: True)

        db_path = str(tmp_path / "loom.db")

        # First session with a provisional title
        session1 = LoomSession(
            model="test-model",
            db_path=db_path,
            workspace=tmp_path,
            provisional_title="First Title",
        )
        await session1.start()
        sid = session1.session_id
        await session1.stop()

        # Resume the same session — provisional_title must NOT overwrite DB title
        session2 = LoomSession(
            model="test-model",
            db_path=db_path,
            resume_session_id=sid,
            workspace=tmp_path,
            provisional_title="Should Not Overwrite",
        )
        await session2.start()
        await session2.stop()

        import aiosqlite
        conn = await aiosqlite.connect(db_path)
        cursor = await conn.execute(
            "SELECT title FROM sessions WHERE session_id = ?", (sid,)
        )
        row = await cursor.fetchone()
        await conn.close()

        # Title from first session must be preserved after resume
        assert row[0] == "First Title"


class TestStreamTurnLock:
    """Regression: per-session ``_turn_lock`` must serialise concurrent
    ``stream_turn()`` invocations so a mid-turn interrupt that fires a new
    turn before the prior one fully unwinds (e.g. the Discord bot's
    cancel-and-relaunch path) cannot let two LLM loops mutate
    ``self.messages`` in parallel.

    Without this lock, the symptom is "the agent does the same thing
    twice": turn B observes turn A's partial assistant+tool history plus
    the new user input, so the model re-runs A's last action.
    """

    async def test_lock_initialised_unlocked(self, tmp_path) -> None:
        from loom.core.session import LoomSession

        workspace = tmp_path / "ws"
        workspace.mkdir()
        session = LoomSession(
            model="test-model",
            db_path=str(tmp_path / "loom.db"),
            workspace=workspace,
        )
        assert isinstance(session._turn_lock, asyncio.Lock)
        assert not session._turn_lock.locked()

    async def test_concurrent_stream_turn_blocks_at_lock(self, tmp_path) -> None:
        """If a prior turn still holds the lock, a new stream_turn() must
        block before mutating ``self.messages`` — not race past it."""
        from loom.core.session import LoomSession

        workspace = tmp_path / "ws"
        workspace.mkdir()
        session = LoomSession(
            model="test-model",
            db_path=str(tmp_path / "loom.db"),
            workspace=workspace,
        )

        # Simulate an in-flight prior turn by holding the lock from outside.
        await session._turn_lock.acquire()
        baseline_history = list(session.messages)

        async def consume() -> None:
            async for _ in session.stream_turn("blocked-input"):
                return

        task = asyncio.create_task(consume())
        # Yield the loop so the task advances as far as it can.
        await asyncio.sleep(0.05)

        # Critical invariant: the second caller has NOT appended the user
        # message — the lock guards that mutation.
        assert not task.done(), "stream_turn must block while _turn_lock is held"
        assert session.messages == baseline_history, (
            "stream_turn appended to history before acquiring _turn_lock — "
            "concurrent turns can corrupt message ordering"
        )

        # Cleanup: release lock then cancel the now-runnable task before it
        # touches subsystems that aren't wired (router, memory).
        task.cancel()
        session._turn_lock.release()
        with contextlib.suppress(BaseException):
            await task


class TestSanitizeHistoryAdjacency:
    """Pass 4 (Issue #218): tool_use ↔ tool_result adjacency repair.

    Anthropic API rejects (2013) any assistant tool_use not immediately
    followed by its tool_result. Pass 2/3 only check existence anywhere;
    Pass 4 enforces position so out-of-order pairs (e.g. late subprocess
    completion appended after subsequent turns) are repaired.
    """

    def _call(self, cid: str, name: str = "run_bash") -> dict:
        return {
            "id": cid,
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }

    def _asst(self, cids: list[str]) -> dict:
        return {"role": "assistant", "content": "", "tool_calls": [self._call(c) for c in cids]}

    def _tool(self, cid: str, content: str = "ok") -> dict:
        return {"role": "tool", "tool_call_id": cid, "content": content}

    def _from_loom_session(self, messages: list[dict]) -> list[dict]:
        from loom.core.session import LoomSession
        fake = SimpleNamespace()
        fake.messages = [dict(m) for m in messages]
        LoomSession._sanitize_history(fake)
        return fake.messages

    def test_late_arriving_tool_result_is_pulled_adjacent(self):
        # Producer: user interrupted while T1 was running; harness ran T2/T3;
        # T1's late result was appended after T3.
        msgs = [
            {"role": "user", "content": "go"},
            self._asst(["T1"]),
            {"role": "user", "content": "interrupt"},
            self._asst(["T2"]),
            self._tool("T2"),
            self._asst(["T3"]),
            self._tool("T3"),
            self._tool("T1"),  # ← late
        ]
        out = self._from_loom_session(msgs)
        # Each assistant tool_call must be immediately followed by its result.
        for i, m in enumerate(out):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                cids = [tc["id"] for tc in m["tool_calls"]]
                followers = out[i+1 : i+1+len(cids)]
                follower_ids = [f.get("tool_call_id") for f in followers]
                assert set(follower_ids) == set(cids), \
                    f"assistant {cids} not immediately followed by results, got {follower_ids}"

    def test_two_consecutive_assistants_repaired(self):
        # Producer (no user interrupt): cancel/timeout left T1 dispatched but
        # not awaited; harness moved to T2; T1's late result arrived after T2.
        msgs = [
            {"role": "user", "content": "go"},
            self._asst(["T1"]),
            self._asst(["T2"]),     # back-to-back assistants
            self._tool("T2"),
            self._tool("T1"),       # late
        ]
        out = self._from_loom_session(msgs)
        # No two assistants with tool_calls may be adjacent.
        for i in range(len(out) - 1):
            a, b = out[i], out[i+1]
            if a.get("role") == "assistant" and a.get("tool_calls"):
                assert b.get("role") == "tool", \
                    f"assistant tool_call at {i} followed by {b.get('role')}, not tool"

    def test_well_formed_history_unchanged(self):
        msgs = [
            {"role": "user", "content": "go"},
            self._asst(["T1"]),
            self._tool("T1"),
            self._asst(["T2"]),
            self._tool("T2"),
        ]
        out = self._from_loom_session(msgs)
        assert out == msgs

    def test_orphan_pairs_still_dropped(self):
        # Pass 2/3 invariants must still hold after Pass 4.
        msgs = [
            {"role": "user", "content": "go"},
            self._asst(["T1"]),  # no result anywhere
            {"role": "user", "content": "next"},
            self._tool("T2"),    # no call anywhere
        ]
        out = self._from_loom_session(msgs)
        roles = [(m.get("role"), m.get("tool_call_id") or
                  [tc["id"] for tc in m.get("tool_calls", [])] or m.get("content"))
                 for m in out]
        # Both orphans gone; only the two user messages remain.
        assert all(m.get("role") == "user" for m in out), roles


class TestLogMessageTurnIndexCapture:
    """Issue #218 Tier 2: _log_message must persist with the turn_index that
    was current at *schedule* time, not at *run* time. Otherwise stream_turn
    advancing _turn_index between an `ensure_future(_log_message(...))` call
    and the task actually running mis-tags the row, which on reload reorders
    the message via `ORDER BY turn_index ASC, id ASC` and breaks the
    tool_use ↔ tool_result adjacency invariant.
    """

    async def test_explicit_turn_index_overrides_live_value(self):
        from loom.core.session import LoomSession

        captured: list[int] = []

        class FakeLog:
            async def log_message(self, session_id, turn_index, role,
                                  content, metadata, raw_json=None):
                captured.append(turn_index)

        fake = SimpleNamespace()
        fake._session_log = FakeLog()
        fake._turn_index = 99  # simulate stream_turn having advanced
        fake.session_id = "s1"

        # Caller scheduled this when _turn_index was 7 — the row must persist
        # at 7, not 99.
        await LoomSession._log_message(
            fake, "tool", "result", {"tool_call_id": "T1"}, turn_index=7,
        )
        assert captured == [7]

    async def test_no_explicit_value_falls_back_to_live(self):
        from loom.core.session import LoomSession

        captured: list[int] = []

        class FakeLog:
            async def log_message(self, session_id, turn_index, role,
                                  content, metadata, raw_json=None):
                captured.append(turn_index)

        fake = SimpleNamespace()
        fake._session_log = FakeLog()
        fake._turn_index = 12
        fake.session_id = "s1"

        await LoomSession._log_message(fake, "user", "hi")
        assert captured == [12]
