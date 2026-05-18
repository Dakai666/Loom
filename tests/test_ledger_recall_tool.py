"""Tests for the ledger_recall tool factory (Issue #385).

Covers:
- input validation (no ledger, missing dimensions, bad limit/dates)
- EventQuery wiring (session_id / skill_id / verdict / time range filters)
- output format inference (narrative for session/correlation, summary
  for skill/tool/time, explicit override)
- session_log cross-database join (user messages quoted in narrative)
- raw escape hatch with truncation
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.core.ledger import (
    LedgerEmitter,
    LedgerStore,
    ToolLifecyclePayload,
    TurnEndPayload,
    TurnStartPayload,
)
from loom.platform.cli.tools import make_ledger_recall_tool


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def ledger(tmp_path: Path):
    s = LedgerStore(
        db_path=tmp_path / "ledger.db", blob_dir=tmp_path / "blobs"
    )
    await s.open()
    try:
        yield s
    finally:
        await s.close()


class FakeSessionLog:
    """In-memory stand-in for MemoryFacade.session_log used by the tool.

    Only needs to satisfy ``messages_between(since, until, *, session_id,
    limit)`` returning the same row shape as the real implementation.
    """

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def messages_between(
        self, since, until=None, *, session_id=None, limit=100,
    ):
        out = []
        for r in self._rows:
            if session_id and r["session_id"] != session_id:
                continue
            created = datetime.fromisoformat(r["created_at"])
            if created < since:
                continue
            if until is not None and created >= until:
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return out


class FakeMemory:
    def __init__(self, session_log=None) -> None:
        self.session_log = session_log


def _call(args: dict) -> ToolCall:
    return ToolCall(
        tool_name="ledger_recall",
        args=args,
        trust_level=TrustLevel.SAFE,
        session_id="sess_x",
    )


async def _seed_turn(
    emitter: LedgerEmitter,
    *,
    turn_id: str,
    base_ts: float,
    tools: list[tuple[str, str | None]],
    outcome: str = "clean",
    skill_id: str | None = None,
) -> None:
    """Seed one turn: turn_start → tool ENDs → turn_end."""
    await emitter.emit(
        "turn_start",
        TurnStartPayload(
            prompt_stack_hash=f"hash_{turn_id}",
            prompt_stack_components={},
        ),
        turn_id=turn_id,
        event_id=f"evt_ts_{turn_id}",
        timestamp=base_ts,
    )
    for i, (tname, err) in enumerate(tools):
        await emitter.emit_tool_lifecycle(
            payload=ToolLifecyclePayload(
                phase="END",
                tool_name=tname,
                tool_call_id=f"call_{turn_id}_{i}",
                args_digest=f"sha256:{turn_id}{i}",
                error=err,
                skill_id=skill_id if tname == "load_skill" else None,
            ),
            event_id=f"evt_tool_{turn_id}_{i}",
            timestamp=base_ts + 0.5 + i * 0.5,
            turn_id=turn_id,
        )
    await emitter.emit(
        "turn_end",
        TurnEndPayload(
            outcome=outcome,
            duration_ms=int(2000 + len(tools) * 500),
            token_usage={},
        ),
        turn_id=turn_id,
        event_id=f"evt_te_{turn_id}",
        timestamp=base_ts + 2.0 + len(tools) * 0.5,
    )


# ── Input validation ──────────────────────────────────────────────────


async def test_no_ledger_returns_error():
    tool = make_ledger_recall_tool(None, None)
    result = await tool.executor(_call({"session_id": "x"}))
    assert result.success is False
    assert "Ledger is not available" in (result.error or "")


async def test_no_dimension_rejected(ledger: LedgerStore):
    tool = make_ledger_recall_tool(ledger, FakeMemory())
    result = await tool.executor(_call({}))
    assert result.success is False
    assert "at least one dimension" in (result.error or "")


async def test_bad_limit_rejected(ledger: LedgerStore):
    tool = make_ledger_recall_tool(ledger, FakeMemory())
    result = await tool.executor(
        _call({"session_id": "s", "limit": "not_an_int"})
    )
    assert result.success is False
    assert "limit" in (result.error or "").lower()


async def test_bad_date_rejected(ledger: LedgerStore):
    tool = make_ledger_recall_tool(ledger, FakeMemory())
    result = await tool.executor(_call({"since": "not-a-date"}))
    assert result.success is False
    assert "datetime" in (result.error or "").lower()


async def test_trust_level_safe():
    tool = make_ledger_recall_tool(None, None)
    assert tool.trust_level == TrustLevel.SAFE


# ── Empty window ──────────────────────────────────────────────────────


async def test_empty_session_renders_fallback(ledger: LedgerStore):
    tool = make_ledger_recall_tool(ledger, FakeMemory())
    result = await tool.executor(_call({"session_id": "no_such_session"}))
    assert result.success is True
    assert "沒有可回憶" in result.output


# ── Narrative path (session_id) ───────────────────────────────────────


async def test_narrative_includes_user_messages_via_session_log(
    ledger: LedgerStore,
):
    base = time.time()
    emitter = LedgerEmitter(ledger, session_id="sess_narrative")
    await _seed_turn(
        emitter,
        turn_id="turn_one",
        base_ts=base,
        tools=[("run_bash", None), ("read_file", None)],
    )

    user_ts = datetime.fromtimestamp(
        base + 0.2, tz=timezone.utc,
    ).isoformat()
    fake_log = FakeSessionLog([
        {
            "session_id": "sess_narrative",
            "turn_index": 0,
            "role": "user",
            "content": "幫我跑一下測試然後讀 README",
            "raw_json": None,
            "metadata": {},
            "created_at": user_ts,
        },
    ])
    tool = make_ledger_recall_tool(ledger, FakeMemory(fake_log))
    result = await tool.executor(_call({"session_id": "sess_narrative"}))

    assert result.success is True
    assert "session sess_narrative" in result.output
    assert "第 1 輪" in result.output
    assert "你說：幫我跑一下測試然後讀 README" in result.output
    assert "run_bash" in result.output
    assert "read_file" in result.output


async def test_narrative_works_without_session_log(ledger: LedgerStore):
    base = time.time()
    emitter = LedgerEmitter(ledger, session_id="sess_no_log")
    await _seed_turn(
        emitter,
        turn_id="turn_nolog",
        base_ts=base,
        tools=[("run_bash", None)],
    )

    tool = make_ledger_recall_tool(ledger, None)  # no memory facade
    result = await tool.executor(_call({"session_id": "sess_no_log"}))
    assert result.success is True
    assert "沒有捕捉到你的訊息" in result.output
    assert "run_bash" in result.output


async def test_narrative_failure_surfaced(ledger: LedgerStore):
    base = time.time()
    emitter = LedgerEmitter(ledger, session_id="sess_fail")
    await _seed_turn(
        emitter,
        turn_id="turn_f",
        base_ts=base,
        tools=[("opencli", "target index not found")],
        outcome="retry",
    )

    tool = make_ledger_recall_tool(ledger, FakeMemory())
    result = await tool.executor(_call({"session_id": "sess_fail"}))
    assert result.success is True
    assert "失敗：target index not found" in result.output
    assert "turn 結局 retry" in result.output


# ── Summary path (skill_id / tool_name / time range) ──────────────────


async def test_summary_default_for_tool_name(ledger: LedgerStore):
    base = time.time()
    emitter = LedgerEmitter(ledger, session_id="sess_sum")
    await _seed_turn(
        emitter, turn_id="turn_s1", base_ts=base,
        tools=[("run_bash", None), ("run_bash", "boom")],
    )
    await _seed_turn(
        emitter, turn_id="turn_s2", base_ts=base + 100,
        tools=[("run_bash", None)],
    )

    tool = make_ledger_recall_tool(ledger, FakeMemory())
    result = await tool.executor(_call({"tool_name": "run_bash"}))
    assert result.success is True
    assert "## 摘要" in result.output
    assert "run_bash" in result.output
    assert "成功 2" in result.output
    assert "失敗 1" in result.output


async def test_summary_for_skill_id(ledger: LedgerStore):
    base = time.time()
    emitter = LedgerEmitter(ledger, session_id="sess_skill")
    await _seed_turn(
        emitter, turn_id="turn_sk", base_ts=base,
        tools=[("load_skill", None)], skill_id="my_skill",
    )

    tool = make_ledger_recall_tool(ledger, FakeMemory())
    result = await tool.executor(_call({"skill_id": "my_skill"}))
    assert result.success is True
    assert "## 摘要" in result.output


# ── Output format override ────────────────────────────────────────────


async def test_explicit_narrative_overrides_summary_default(
    ledger: LedgerStore,
):
    base = time.time()
    emitter = LedgerEmitter(ledger, session_id="sess_ovr")
    await _seed_turn(
        emitter, turn_id="turn_ov", base_ts=base,
        tools=[("run_bash", None)],
    )
    tool = make_ledger_recall_tool(ledger, FakeMemory())
    # tool_name alone would auto-pick summary; explicit narrative wins
    result = await tool.executor(
        _call({"tool_name": "run_bash", "output": "narrative"})
    )
    assert result.success is True
    assert "## session" in result.output
    assert "第 1 輪" in result.output


async def test_raw_output_truncates(ledger: LedgerStore):
    base = time.time()
    emitter = LedgerEmitter(ledger, session_id="sess_raw")
    # 5 turns × 3 tools each = lots of events
    for i in range(5):
        await _seed_turn(
            emitter, turn_id=f"turn_r{i}", base_ts=base + i * 10,
            tools=[("tool_a", None), ("tool_b", None), ("tool_c", None)],
        )

    tool = make_ledger_recall_tool(ledger, FakeMemory())
    result = await tool.executor(
        _call({"session_id": "sess_raw", "output": "raw", "limit": 3})
    )
    assert result.success is True
    assert "Raw events" in result.output
    assert "showing first 3" in result.output


async def test_verdict_filter_narrows_results(ledger: LedgerStore):
    """Sanity check that EventQuery.where(verdict=...) is wired through."""
    base = time.time()
    emitter = LedgerEmitter(ledger, session_id="sess_verdict")
    # Seed two turns; only one has a turn_end with non-clean outcome.
    await _seed_turn(
        emitter, turn_id="turn_clean", base_ts=base,
        tools=[("a", None)], outcome="clean",
    )
    await _seed_turn(
        emitter, turn_id="turn_retry", base_ts=base + 50,
        tools=[("b", None)], outcome="retry",
    )

    # verdict isn't populated by these payloads (TurnEnd uses 'outcome'),
    # so this proves the filter is applied even when zero events match.
    tool = make_ledger_recall_tool(ledger, FakeMemory())
    result = await tool.executor(_call({"verdict": "PASS"}))
    assert result.success is True
    assert "沒有可回憶" in result.output


# ── PR #386 review fixes ──────────────────────────────────────────────


async def test_narrative_rehydrates_full_turn_when_narrowing_filter_applied(
    ledger: LedgerStore,
):
    """Bug: session_id + tool_name=X filtered out turn_start / turn_end /
    user message preceding the first matching tool, leaving narrative
    without user intent or outcome.

    Fix: when narrative + narrowing + scope, re-pull full turn context
    for the matching turn_ids using scope filters only.
    """
    base = time.time()
    emitter = LedgerEmitter(ledger, session_id="sess_hydrate")
    # Turn with: turn_start, two tools (one matched, one not), turn_end
    # with non-clean outcome.
    await _seed_turn(
        emitter,
        turn_id="turn_h",
        base_ts=base,
        tools=[("write_file", None), ("run_bash", None)],
        outcome="retry",
    )
    # A second turn with NO run_bash — must not appear in narrative.
    await _seed_turn(
        emitter,
        turn_id="turn_skip",
        base_ts=base + 50,
        tools=[("read_file", None)],
    )

    # User message is at base + 0.1 — BEFORE the first run_bash tool (at
    # base + 1.0 in this seed pattern). Without the re-query, narrative
    # would search session_log from base+1.0 onward and miss the message.
    user_ts = datetime.fromtimestamp(base + 0.1, tz=timezone.utc).isoformat()
    fake_log = FakeSessionLog([{
        "session_id": "sess_hydrate",
        "turn_index": 0,
        "role": "user",
        "content": "請幫我跑測試",
        "raw_json": None,
        "metadata": {},
        "created_at": user_ts,
    }])

    tool = make_ledger_recall_tool(ledger, FakeMemory(fake_log))
    result = await tool.executor(_call({
        "session_id": "sess_hydrate",
        "tool_name": "run_bash",
    }))

    assert result.success is True
    # User message must surface (this is the regression we're fixing).
    assert "你說：請幫我跑測試" in result.output
    # Turn outcome must surface.
    assert "turn 結局 retry" in result.output
    # The other (non-matching) tool from the same turn becomes visible
    # because narrative shows full turn context.
    assert "write_file" in result.output
    assert "run_bash" in result.output
    # The non-matching turn must not appear.
    assert "turn_skip" not in result.output
    assert "read_file" not in result.output


async def test_truncation_warning_in_summary(
    ledger: LedgerStore, monkeypatch,
):
    """When ledger fetch hits the per-query cap, summary prepends a
    warning so the agent doesn't cite partial totals as authoritative.

    Monkeypatched cap rather than seeding 1000+ real events — same code
    path, faster test.
    """
    import loom.platform.cli.tools as tools_mod
    monkeypatch.setattr(tools_mod, "_LEDGER_RECALL_EVENT_CAP", 3)

    base = time.time()
    emitter = LedgerEmitter(ledger, session_id="sess_trunc")
    # 5 tool ENDs > cap of 3
    for i in range(5):
        await emitter.emit_tool_lifecycle(
            turn_id=f"turn_t{i}",
            payload=ToolLifecyclePayload(
                phase="END",
                tool_name="run_bash",
                tool_call_id=f"call_{i}",
                args_digest=f"sha256:{i}",
            ),
            event_id=f"evt_{i}",
            timestamp=base + i * 0.5,
        )

    tool = make_ledger_recall_tool(ledger, FakeMemory())
    result = await tool.executor(_call({"tool_name": "run_bash"}))
    assert result.success is True
    assert result.output.startswith("[!]")
    assert "ledger 查詢上限" in result.output
    # Still renders the summary body
    assert "## 摘要" in result.output


async def test_no_truncation_warning_when_under_cap(ledger: LedgerStore):
    """Sanity: with comfortable headroom, warning must NOT appear."""
    base = time.time()
    emitter = LedgerEmitter(ledger, session_id="sess_ok")
    await _seed_turn(
        emitter, turn_id="turn_ok", base_ts=base,
        tools=[("run_bash", None)],
    )
    tool = make_ledger_recall_tool(ledger, FakeMemory())
    result = await tool.executor(_call({"tool_name": "run_bash"}))
    assert result.success is True
    assert not result.output.startswith("[!]")
