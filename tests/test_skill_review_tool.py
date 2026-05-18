"""Tests for the skill_review tool factory (doc/54 §5 P0-5)."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.core.ledger import (
    LedgerEmitter,
    LedgerStore,
    ToolLifecyclePayload,
    async_correlation_scope,
    async_turn_scope,
)
from loom.platform.cli.tools import make_skill_review_tool


@pytest_asyncio.fixture
async def ledger(tmp_path: Path) -> LedgerStore:
    s = LedgerStore(db_path=tmp_path / "ledger.db", blob_dir=tmp_path / "blobs")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


def _call(args: dict) -> ToolCall:
    return ToolCall(
        tool_name="skill_review",
        args=args,
        trust_level=TrustLevel.SAFE,
        session_id="sess_test",
    )


async def test_skill_id_required(ledger: LedgerStore) -> None:
    tool = make_skill_review_tool(ledger)
    result = await tool.executor(_call({}))
    assert result.success is False
    assert "skill_id required" in (result.error or "")


async def test_no_ledger_returns_error() -> None:
    tool = make_skill_review_tool(None)
    result = await tool.executor(_call({"skill_id": "code_weaver"}))
    assert result.success is False
    assert "ledger" in (result.error or "").lower()


async def test_invalid_days_rejected(ledger: LedgerStore) -> None:
    tool = make_skill_review_tool(ledger)
    result = await tool.executor(_call({"skill_id": "x", "days": "abc"}))
    assert result.success is False
    assert "days" in (result.error or "").lower()

    result = await tool.executor(_call({"skill_id": "x", "days": -1}))
    assert result.success is False


async def test_empty_window_renders_clean_output(ledger: LedgerStore) -> None:
    tool = make_skill_review_tool(ledger)
    result = await tool.executor(_call({"skill_id": "no_such_skill"}))
    assert result.success is True
    assert "Skill review: no_such_skill" in result.output
    assert "Loads: 0" in result.output
    assert "no activations" in result.output


async def test_rendered_output_contains_episodes(ledger: LedgerStore) -> None:
    emitter = LedgerEmitter(ledger, session_id="sess_test")
    async with async_turn_scope("turn_A"), async_correlation_scope("c_A"):
        await emitter.emit_tool_lifecycle(
            payload=ToolLifecyclePayload(
                phase="END",
                tool_name="load_skill",
                tool_call_id="call_1",
                args_digest="sha256:1",
                skill_id="code_weaver",
            ),
            event_id="evt_load_end_1",
        )
        await emitter.emit_tool_lifecycle(
            payload=ToolLifecyclePayload(
                phase="END",
                tool_name="write_file",
                tool_call_id="call_2",
                args_digest="sha256:2",
            ),
            event_id="evt_write_end_1",
        )

    tool = make_skill_review_tool(ledger)
    result = await tool.executor(_call({"skill_id": "code_weaver"}))
    assert result.success is True
    assert "Loads: 1" in result.output
    assert "Episode 1" in result.output
    assert "write_file" in result.output


async def test_trust_level_safe(ledger: LedgerStore) -> None:
    """Read-only tool must be SAFE — no confirmation prompts."""
    tool = make_skill_review_tool(ledger)
    assert tool.trust_level == TrustLevel.SAFE


async def test_render_surfaces_inferred_unload_reason(
    ledger: LedgerStore,
) -> None:
    """When the agent forgets to unload, the rendered output must flag
    the boundary as inferred so reviewers spot the bookend gap."""
    emitter = LedgerEmitter(ledger, session_id="sess_test")
    async with async_turn_scope("turn_A"), async_correlation_scope("c_A"):
        await emitter.emit_tool_lifecycle(
            payload=ToolLifecyclePayload(
                phase="END",
                tool_name="load_skill",
                tool_call_id="call_1",
                args_digest="sha256:1",
                skill_id="code_weaver",
            ),
            event_id="evt_load_end_orphan",
        )

    tool = make_skill_review_tool(ledger)
    result = await tool.executor(_call({"skill_id": "code_weaver"}))
    assert result.success is True
    assert "inferred: no_unload_in_window" in result.output
