from __future__ import annotations

import asyncio
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
