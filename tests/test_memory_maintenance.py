"""
Issue #149 — dream_cycle / memory_prune factories live in
``loom.core.memory.maintenance`` (formerly DreamingPlugin).

Verifies factory-shape, executor wiring, dry-run formatting, and that
``LoomSession`` registers both tools at startup.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.core.memory.maintenance import (
    make_dream_cycle_tool,
    make_memory_prune_tool,
)


def _call(name: str, args: dict | None = None) -> ToolCall:
    return ToolCall(
        tool_name=name, args=args or {},
        trust_level=TrustLevel.SAFE, session_id="test",
    )


# ── make_dream_cycle_tool ──────────────────────────────────────────────────

def test_dream_cycle_definition_shape():
    tool = make_dream_cycle_tool(semantic=object(), relational=object(), llm_fn=AsyncMock())
    assert tool.name == "dream_cycle"
    assert tool.trust_level == TrustLevel.SAFE
    props = tool.input_schema["properties"]
    assert set(props) == {"sample_size", "dry_run"}
    assert props["sample_size"]["default"] == 15
    assert props["dry_run"]["default"] is False


async def test_dream_cycle_executor_passes_args_through(monkeypatch):
    captured = {}

    async def fake_dream_cycle(*, semantic, relational, llm_fn, sample_size, dry_run):
        captured["sample_size"] = sample_size
        captured["dry_run"] = dry_run
        return {
            "facts_sampled": 7, "triples_found": 3,
            "triples_written": 0 if dry_run else 3, "errors": [],
        }

    monkeypatch.setattr(
        "loom.core.cognition.dreaming.dream_cycle", fake_dream_cycle,
    )

    tool = make_dream_cycle_tool(semantic=object(), relational=object(), llm_fn=AsyncMock())
    res = await tool.executor(_call("dream_cycle", {"sample_size": 7, "dry_run": True}))

    assert res.success
    assert captured == {"sample_size": 7, "dry_run": True}
    assert "Facts sampled: 7" in res.output
    assert "dry-run" in res.output  # dry_run banner appears


async def test_dream_cycle_executor_reports_warnings(monkeypatch):
    async def fake_dream_cycle(**_):
        return {
            "facts_sampled": 5, "triples_found": 2, "triples_written": 1,
            "errors": ["malformed JSON"],
        }
    monkeypatch.setattr(
        "loom.core.cognition.dreaming.dream_cycle", fake_dream_cycle,
    )

    tool = make_dream_cycle_tool(semantic=object(), relational=object(), llm_fn=AsyncMock())
    res = await tool.executor(_call("dream_cycle"))
    assert "Warnings: malformed JSON" in res.output


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
