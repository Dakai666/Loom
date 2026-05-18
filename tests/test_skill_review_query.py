"""Tests for query_skill_ledger (doc/54 §5 P0-4).

Verifies the pure aggregation function shapes skill-scoped ledger events
into SkillUsageDigest correctly. No LLM, no scoring — just evidence shape.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import pytest_asyncio

from loom.core.ledger import (
    LedgerEmitter,
    LedgerStore,
    ToolLifecyclePayload,
    TurnEndPayload,
    MemoryOpPayload,
    JudgeVerdictPayload,
    async_correlation_scope,
    async_turn_scope,
)
from loom.core.skill_review import query_skill_ledger


@pytest_asyncio.fixture
async def ledger(tmp_path: Path) -> LedgerStore:
    s = LedgerStore(db_path=tmp_path / "ledger.db", blob_dir=tmp_path / "blobs")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
def emitter(ledger: LedgerStore) -> LedgerEmitter:
    return LedgerEmitter(ledger, session_id="sess_test")


async def _emit_load(
    emitter: LedgerEmitter, *, skill: str, suffix: str
) -> None:
    """Emit a BEGIN/END pair for load_skill(skill)."""
    await emitter.emit_tool_lifecycle(
        payload=ToolLifecyclePayload(
            phase="BEGIN",
            tool_name="load_skill",
            tool_call_id=f"call_{suffix}",
            args_digest=f"sha256:{suffix}",
            skill_id=skill,
        ),
        event_id=f"evt_load_begin_{suffix}",
    )
    await emitter.emit_tool_lifecycle(
        payload=ToolLifecyclePayload(
            phase="END",
            tool_name="load_skill",
            tool_call_id=f"call_{suffix}",
            args_digest=f"sha256:{suffix}",
            result_digest=f"sha256:res_{suffix}",
            skill_id=skill,
        ),
        event_id=f"evt_load_end_{suffix}",
        parent_event_id=f"evt_load_begin_{suffix}",
    )


async def _emit_unload(
    emitter: LedgerEmitter, *, skill: str, suffix: str
) -> None:
    await emitter.emit_tool_lifecycle(
        payload=ToolLifecyclePayload(
            phase="END",
            tool_name="unload_skill",
            tool_call_id=f"call_un_{suffix}",
            args_digest=f"sha256:un_{suffix}",
            skill_id=skill,
        ),
        event_id=f"evt_unload_end_{suffix}",
    )


async def test_empty_window_returns_zero_digest(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    digest = await query_skill_ledger(ledger, "no_such_skill")
    assert digest.load_count == 0
    assert digest.unload_count == 0
    assert digest.sessions == ()
    assert digest.episodes == []


async def test_load_unload_pair_in_single_turn(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    async with async_turn_scope("turn_A"), async_correlation_scope("c_A"):
        await _emit_load(emitter, skill="code_weaver", suffix="1")
        # A tool call in between — should land in the episode.
        await emitter.emit_tool_lifecycle(
            payload=ToolLifecyclePayload(
                phase="END",
                tool_name="write_file",
                tool_call_id="call_write_1",
                args_digest="sha256:w1",
                result_digest="sha256:rw1",
            ),
            event_id="evt_write_end_1",
        )
        await _emit_unload(emitter, skill="code_weaver", suffix="1")

    digest = await query_skill_ledger(ledger, "code_weaver")

    assert digest.skill_id == "code_weaver"
    assert digest.load_count == 1
    assert digest.unload_count == 1
    assert digest.sessions == ("sess_test",)
    assert len(digest.episodes) == 1

    ep = digest.episodes[0]
    assert ep.session_id == "sess_test"
    assert ep.turn_id == "turn_A"
    assert ep.unloaded_at is not None
    assert ep.unloaded_at > ep.loaded_at
    # Should contain the write_file event between load and unload.
    tool_names = [
        e.payload.get("tool_name") for e in ep.events_after_load
        if e.event_type == "tool_lifecycle"
    ]
    assert "write_file" in tool_names
    # Should NOT contain its own load END (it's the activation marker).
    assert all(e.event_id != "evt_load_end_1" for e in ep.events_after_load)


async def test_other_skill_events_excluded(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    async with async_turn_scope("turn_A"), async_correlation_scope("c_A"):
        await _emit_load(emitter, skill="code_weaver", suffix="1")
    async with async_turn_scope("turn_B"), async_correlation_scope("c_B"):
        await _emit_load(emitter, skill="news_aggregator", suffix="2")

    digest = await query_skill_ledger(ledger, "code_weaver")
    assert digest.load_count == 1
    assert len(digest.episodes) == 1
    assert digest.episodes[0].turn_id == "turn_A"


async def test_window_filters_by_timestamp(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    async with async_turn_scope("turn_old"), async_correlation_scope("c_old"):
        await _emit_load(emitter, skill="code_weaver", suffix="old")
    # Force window to exclude all current events.
    future = time.time() + 10_000
    digest = await query_skill_ledger(
        ledger, "code_weaver",
        since_ts=future,
        until_ts=future + 100,
    )
    assert digest.load_count == 0


async def test_episode_captures_memory_op_feedback(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    """memory_op write after load should land in events_after_load — this
    is how feedback density gets surfaced to the agent."""
    async with async_turn_scope("turn_A"), async_correlation_scope("c_A"):
        await _emit_load(emitter, skill="code_weaver", suffix="1")
        await emitter.emit_memory_op(
            payload=MemoryOpPayload(
                operation="write",
                memory_id="mem_feedback",
                trust_tier="user_explicit",
            ),
        )

    digest = await query_skill_ledger(ledger, "code_weaver")
    assert len(digest.episodes) == 1
    types = [e.event_type for e in digest.episodes[0].events_after_load]
    assert "memory_op" in types


async def test_turn_outcome_attached_to_episode(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    async with async_turn_scope("turn_A"), async_correlation_scope("c_A"):
        await _emit_load(emitter, skill="code_weaver", suffix="1")
        await emitter.emit_turn_end(
            payload=TurnEndPayload(
                outcome="error",
                duration_ms=1234,
                token_usage={"in": 100, "out": 50},
            ),
        )

    digest = await query_skill_ledger(ledger, "code_weaver")
    assert digest.episodes[0].turn_outcome == "error"


async def test_events_per_episode_cap(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    async with async_turn_scope("turn_A"), async_correlation_scope("c_A"):
        await _emit_load(emitter, skill="code_weaver", suffix="1")
        for i in range(5):
            await emitter.emit_tool_lifecycle(
                payload=ToolLifecyclePayload(
                    phase="END",
                    tool_name=f"tool_{i}",
                    tool_call_id=f"call_t_{i}",
                    args_digest=f"sha256:t{i}",
                ),
                event_id=f"evt_t_end_{i}",
            )

    digest = await query_skill_ledger(
        ledger, "code_weaver", max_events_per_episode=2
    )
    ep = digest.episodes[0]
    assert ep.truncated is True
    assert len(ep.events_after_load) == 2


async def test_sessions_distinct_and_sorted(
    ledger: LedgerStore, ledger_fixture_path: Path = None
) -> None:
    """Events from multiple sessions for the same skill aggregate."""
    e_a = LedgerEmitter(ledger, session_id="sess_a")
    e_b = LedgerEmitter(ledger, session_id="sess_b")

    async with async_turn_scope("turn_1"), async_correlation_scope("c1"):
        await _emit_load(e_a, skill="code_weaver", suffix="a1")
    async with async_turn_scope("turn_2"), async_correlation_scope("c2"):
        await _emit_load(e_b, skill="code_weaver", suffix="b1")

    digest = await query_skill_ledger(ledger, "code_weaver")
    assert digest.load_count == 2
    assert digest.sessions == ("sess_a", "sess_b")


# ── Cross-turn / orphan-load scenarios — agent commonly forgets unload ──


async def test_unload_in_later_turn_captures_cross_turn_window(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    """Skill loaded in turn A and unloaded in turn C must surface all
    intermediate evidence — agents routinely span turns mid-task."""
    async with async_turn_scope("turn_A"), async_correlation_scope("c_A"):
        await _emit_load(emitter, skill="code_weaver", suffix="1")
    async with async_turn_scope("turn_B"), async_correlation_scope("c_B"):
        # In-between work that the old per-turn query silently lost.
        await emitter.emit_tool_lifecycle(
            payload=ToolLifecyclePayload(
                phase="END",
                tool_name="write_file",
                tool_call_id="call_w_B",
                args_digest="sha256:wB",
                result_digest="sha256:rwB",
            ),
            event_id="evt_w_end_B",
        )
        await emitter.emit_memory_op(
            payload=MemoryOpPayload(
                operation="write",
                memory_id="mem_feedback_B",
                trust_tier="user_explicit",
            ),
        )
    async with async_turn_scope("turn_C"), async_correlation_scope("c_C"):
        await _emit_unload(emitter, skill="code_weaver", suffix="1")

    digest = await query_skill_ledger(ledger, "code_weaver")
    assert digest.load_count == 1
    assert digest.unload_count == 1
    assert len(digest.episodes) == 1
    ep = digest.episodes[0]
    assert ep.unload_inferred is False
    assert ep.unload_inferred_reason is None
    assert ep.unloaded_at is not None and ep.unloaded_at > ep.loaded_at
    tool_names = [
        e.payload.get("tool_name") for e in ep.events_after_load
        if e.event_type == "tool_lifecycle"
    ]
    assert "write_file" in tool_names
    types = [e.event_type for e in ep.events_after_load]
    assert "memory_op" in types


async def test_load_without_unload_marks_inferred_no_boundary(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    """Loaded but never unloaded within the query window — episode stays
    open and flags ``no_unload_in_window``. This is the common agent
    bookend-failure case."""
    async with async_turn_scope("turn_A"), async_correlation_scope("c_A"):
        await _emit_load(emitter, skill="code_weaver", suffix="1")
        await emitter.emit_tool_lifecycle(
            payload=ToolLifecyclePayload(
                phase="END",
                tool_name="write_file",
                tool_call_id="call_w_A",
                args_digest="sha256:wA",
                result_digest="sha256:rwA",
            ),
            event_id="evt_w_end_A",
        )

    digest = await query_skill_ledger(ledger, "code_weaver")
    assert digest.load_count == 1
    assert digest.unload_count == 0
    ep = digest.episodes[0]
    assert ep.unload_inferred is True
    assert ep.unload_inferred_reason == "no_unload_in_window"
    assert ep.unloaded_at is None
    # Despite the open window, in-window evidence is still captured.
    tool_names = [
        e.payload.get("tool_name") for e in ep.events_after_load
        if e.event_type == "tool_lifecycle"
    ]
    assert "write_file" in tool_names


async def test_reload_without_unload_closes_previous_episode(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    """Re-loading the same skill before unloading the prior activation
    is treated as an implicit boundary — the prior episode closes at
    the re-load timestamp and is flagged ``reloaded_without_unload``."""
    async with async_turn_scope("turn_A"), async_correlation_scope("c_A"):
        await _emit_load(emitter, skill="code_weaver", suffix="1")
        await emitter.emit_tool_lifecycle(
            payload=ToolLifecyclePayload(
                phase="END",
                tool_name="write_file",
                tool_call_id="call_w_A",
                args_digest="sha256:wA",
                result_digest="sha256:rwA",
            ),
            event_id="evt_w_end_A",
        )
    async with async_turn_scope("turn_B"), async_correlation_scope("c_B"):
        await _emit_load(emitter, skill="code_weaver", suffix="2")
        await _emit_unload(emitter, skill="code_weaver", suffix="2")

    digest = await query_skill_ledger(ledger, "code_weaver")
    assert digest.load_count == 2
    assert digest.unload_count == 1
    assert len(digest.episodes) == 2

    first, second = digest.episodes
    assert first.unload_inferred is True
    assert first.unload_inferred_reason == "reloaded_without_unload"
    assert first.unloaded_at is not None  # set to the reload timestamp
    # In-window evidence between first load and the reload boundary.
    tool_names = [
        e.payload.get("tool_name") for e in first.events_after_load
        if e.event_type == "tool_lifecycle"
    ]
    assert "write_file" in tool_names

    assert second.unload_inferred is False
    assert second.unload_inferred_reason is None


async def test_nested_skills_each_get_own_window(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    """Loading skill B while skill A is still active is fine — A's
    activation persists until A is itself unloaded; B's load/unload
    events naturally appear inside A's events_after_load as evidence."""
    async with async_turn_scope("turn_A"), async_correlation_scope("c_A"):
        await _emit_load(emitter, skill="code_weaver", suffix="A1")
        await _emit_load(emitter, skill="news_aggregator", suffix="B1")
        await _emit_unload(emitter, skill="news_aggregator", suffix="B1")
        await _emit_unload(emitter, skill="code_weaver", suffix="A1")

    digest_a = await query_skill_ledger(ledger, "code_weaver")
    digest_b = await query_skill_ledger(ledger, "news_aggregator")

    assert digest_a.load_count == 1
    assert digest_a.episodes[0].unload_inferred is False
    # Inner skill's lifecycle events should appear in A's evidence —
    # this is what lets a reviewer notice the nesting pattern.
    inner_tools = {
        e.payload.get("tool_name") for e in digest_a.episodes[0].events_after_load
        if e.event_type == "tool_lifecycle"
        and e.payload.get("skill_id") == "news_aggregator"
    }
    assert {"load_skill", "unload_skill"} <= inner_tools

    assert digest_b.load_count == 1
    assert digest_b.episodes[0].unload_inferred is False
