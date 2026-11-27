"""#708 S4a item 1 — Plugin / Adapter retirement contract (MCP kept intact)."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
from unittest.mock import MagicMock

import pytest

import loom
import loom.extensibility as extensibility
from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.session import LoomSession


@pytest.mark.parametrize("module_name", [
    "loom.extensibility.plugin",
    "loom.extensibility.adapter",
])
def test_retired_plugin_adapter_modules_are_gone(module_name: str) -> None:
    assert importlib.util.find_spec(module_name) is None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


def test_retired_public_surfaces_are_gone() -> None:
    for attr in (
        "tool",
        "register_plugin",
        "_get_default_registry",
        "_get_default_plugin_registry",
        "_default_registry",
        "_default_plugin_registry",
    ):
        assert not hasattr(loom, attr), f"loom.{attr} should be retired"

    for attr in ("AdapterRegistry", "LoomPlugin", "PluginRegistry"):
        assert not hasattr(extensibility, attr), f"loom.extensibility.{attr} should be retired"
        assert attr not in extensibility.__all__

    assert not hasattr(LoomSession, "_load_plugins")


def test_mcp_server_and_client_remain_intact() -> None:
    for attr in (
        "run_mcp_server",
        "LoomMCPClient",
        "MCPServerConfig",
        "load_mcp_servers_into_session",
    ):
        assert hasattr(extensibility, attr), f"loom.extensibility.{attr} must remain"
        assert attr in extensibility.__all__

    from loom.extensibility.mcp_server import _registry_to_mcp_tools, run_mcp_server

    assert inspect.iscoroutinefunction(run_mcp_server)

    async def _noop(call: ToolCall) -> ToolResult:
        return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True, output="ok")

    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="safe_probe", description="Safe", trust_level=TrustLevel.SAFE,
        input_schema={"type": "object", "properties": {}}, executor=_noop,
    ))
    reg.register(ToolDefinition(
        name="guarded_act", description="Guarded", trust_level=TrustLevel.GUARDED,
        input_schema={"type": "object", "properties": {}}, executor=_noop,
    ))
    reg.register(ToolDefinition(
        name="critical_op", description="Critical", trust_level=TrustLevel.CRITICAL,
        input_schema={"type": "object", "properties": {}}, executor=_noop,
    ))

    mcp_tools = _registry_to_mcp_tools(reg)
    names = [t.name for t in mcp_tools]
    assert "safe_probe" in names
    assert "guarded_act" in names
    assert "critical_op" not in names


async def test_session_start_ignores_plugin_files_and_loom_tools(tmp_path, monkeypatch) -> None:
    from loom.core import session as session_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    plugin_dir = home / ".loom" / "plugins"
    plugin_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(session_module, "build_router", lambda *a, **k: MagicMock())
    monkeypatch.setattr(session_module, "_load_loom_config", lambda: {})
    monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {})
    monkeypatch.setattr(session_module, "build_embedding_provider", lambda env, cfg: None)

    sentinel = tmp_path / "executed.flag"
    payload = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n"
    (plugin_dir / "legacy_plugin.py").write_text(payload, encoding="utf-8")
    (workspace / "loom_tools.py").write_text(payload, encoding="utf-8")

    session = LoomSession(
        model="gpt-test",
        db_path=str(tmp_path / "loom.db"),
        workspace=workspace,
    )
    await session.start()
    try:
        assert not sentinel.exists()
    finally:
        await session.stop()
