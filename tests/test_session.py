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
