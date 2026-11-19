"""
Issue #149 — the memory_prune factory lives in
``loom.core.memory.maintenance`` (formerly DreamingPlugin). The dream_cycle
tool that shared this file was retired in #639 (see test_dream_tools_retired).

Verifies factory-shape and executor wiring.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.core.memory.maintenance import make_memory_prune_tool


def _call(name: str, args: dict | None = None) -> ToolCall:
    return ToolCall(
        tool_name=name, args=args or {},
        trust_level=TrustLevel.SAFE, session_id="test",
    )


# ── make_memory_prune_tool ─────────────────────────────────────────────────

def test_memory_prune_definition_shape():
    tool = make_memory_prune_tool(semantic=object())
    assert tool.name == "memory_prune"
    assert tool.trust_level == TrustLevel.SAFE
    props = tool.input_schema["properties"]
    assert set(props) == {"threshold", "dry_run"}
    assert props["threshold"]["default"] == 0.1


async def test_memory_prune_executor_invokes_semantic():
    semantic = AsyncMock()
    semantic.prune_decayed = AsyncMock(return_value={
        "examined": 100, "pruned": 12, "retained": 88,
    })

    tool = make_memory_prune_tool(semantic=semantic)
    res = await tool.executor(_call("memory_prune", {"threshold": 0.2, "dry_run": True}))

    assert res.success
    semantic.prune_decayed.assert_awaited_once_with(threshold=0.2, dry_run=True)
    assert "Examined : 100" in res.output
    assert "Pruned   : 12" in res.output
    assert "dry-run" in res.output


async def test_memory_prune_default_args():
    semantic = AsyncMock()
    semantic.prune_decayed = AsyncMock(return_value={
        "examined": 0, "pruned": 0, "retained": 0,
    })

    tool = make_memory_prune_tool(semantic=semantic)
    await tool.executor(_call("memory_prune"))
    semantic.prune_decayed.assert_awaited_once_with(threshold=0.1, dry_run=False)


# ── Session integration ────────────────────────────────────────────────────

def test_dreaming_plugin_no_longer_importable():
    """Issue #149: extensibility plugin module must be gone."""
    with pytest.raises(ImportError):
        import loom.extensibility.dreaming_plugin  # noqa: F401


def test_dreaming_plugin_not_re_exported():
    import loom.extensibility as ext
    assert not hasattr(ext, "DreamingPlugin")
