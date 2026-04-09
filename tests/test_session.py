from __future__ import annotations

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
        from loom.platform.cli import main as cli_main

        return cli_main

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
        monkeypatch.setattr(session_module.Confirm, "ask", lambda *args, **kwargs: True)

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
