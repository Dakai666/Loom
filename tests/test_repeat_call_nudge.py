"""Repeated identical tool call nudge (Issue #654).

Contract:

* Within one user turn, the 3rd and later occurrence of the same tool called
  with the same arguments and returning the same result carries a
  ``[harness]`` reminder on the tool result the model reads.
* A call whose arguments, result or success differ is never counted.
* The count starts over at the next ``stream_turn``.
* Below the hard limit it is a nudge only: every call runs and the turn
  continues. At the 10th identical occurrence the turn ends — after the
  current batch completes, so every tool call has its result — with
  ``TurnDropped(stop_reason="repeat_call_limit")``.
* Works on both dispatch paths (sequential and parallel), and on JIT-spilled
  outputs whose placeholder differs per call.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import loom as loom_pkg
from loom.core.harness.middleware import JITRetrievalMiddleware, ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.repeat_guard import HARD_LIMIT, RepeatCallTracker

NUDGE = "[harness]"


def _ok(output: str) -> ToolResult:
    return ToolResult(call_id="c", tool_name="t", success=True, output=output)


def _err(error: str) -> ToolResult:
    return ToolResult(call_id="c", tool_name="t", success=False, error=error)


# ── tracker ──────────────────────────────────────────────────────────────────


def test_third_identical_call_is_nudged_and_later_ones_keep_counting():
    t = RepeatCallTracker()
    suffixes = [t.observe("post", {"id": 0}, _err("404 Unknown Channel")) for _ in range(5)]
    assert suffixes[0] == suffixes[1] == ""
    assert all(NUDGE in s for s in suffixes[2:])
    assert "#3" in suffixes[2] and "#5" in suffixes[4]


def test_different_arguments_are_not_repeats():
    t = RepeatCallTracker()
    for q in ("a", "b", "c", "d"):
        assert t.observe("recall", {"query": q}, _ok("nothing")) == ""


def test_argument_order_does_not_hide_a_repeat():
    t = RepeatCallTracker()
    t.observe("recall", {"query": "x", "limit": 5}, _ok("r"))
    t.observe("recall", {"limit": 5, "query": "x"}, _ok("r"))
    assert NUDGE in t.observe("recall", {"query": "x", "limit": 5}, _ok("r"))


def test_a_changed_result_is_not_a_repeat():
    """Polling a job until it finishes is legitimate — the result moves."""
    t = RepeatCallTracker()
    for state in ("queued", "running", "running ", "done"):
        assert t.observe("job_status", {"id": 1}, _ok(state)) == ""


def test_success_and_failure_with_the_same_text_are_different_results():
    t = RepeatCallTracker()
    t.observe("t", {}, _ok("x"))
    t.observe("t", {}, _err("x"))
    assert t.observe("t", {}, _ok("x")) == ""


def test_hard_limit_is_reported_once_reached():
    t = RepeatCallTracker()
    for _ in range(HARD_LIMIT - 1):
        t.observe("post", {"id": 0}, _err("404"))
    assert t.limit_hit is None
    suffix = t.observe("post", {"id": 0}, _err("404"))
    assert "ending this turn" in suffix
    assert t.limit_hit and "post" in t.limit_hit and str(HARD_LIMIT) in t.limit_hit


def test_many_different_calls_never_reach_the_hard_limit():
    t = RepeatCallTracker()
    for i in range(HARD_LIMIT * 3):
        t.observe("recall", {"query": str(i)}, _ok("nothing"))
    assert t.limit_hit is None


def test_reset_clears_the_hard_limit():
    t = RepeatCallTracker()
    for _ in range(HARD_LIMIT):
        t.observe("t", {}, _ok("x"))
    t.reset()
    assert t.limit_hit is None


def test_reset_starts_the_count_over():
    t = RepeatCallTracker()
    for _ in range(3):
        t.observe("t", {}, _ok("x"))
    t.reset()
    assert t.observe("t", {}, _ok("x")) == ""


async def test_spilled_outputs_compare_by_content_not_placeholder(tmp_path: Path):
    """JIT gives each spill a fresh scratchpad ref, so the placeholder text
    differs on every call; identical spilled content must still count."""
    from loom.core.jobs.scratchpad import Scratchpad

    jit = JITRetrievalMiddleware(scratchpad=Scratchpad(), registry=None, threshold_chars=10)
    big = "x" * 100

    async def handler(call: ToolCall) -> ToolResult:
        return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True, output=big)

    call = ToolCall(id="c", tool_name="fetch", args={"u": 1}, trust_level=TrustLevel.SAFE, session_id="s")
    results = [await jit.process(call, handler) for _ in range(3)]
    assert results[0].output != results[1].output  # placeholders differ

    t = RepeatCallTracker()
    suffixes = [t.observe("fetch", {"u": 1}, r) for r in results]
    assert suffixes[:2] == ["", ""] and NUDGE in suffixes[2]


# ── real stream_turn path ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_default_registry():
    registry = loom_pkg._get_default_registry()
    original = dict(registry._tools)
    registry._tools.clear()
    try:
        yield
    finally:
        registry._tools.clear()
        registry._tools.update(original)


async def _session(monkeypatch, tmp_path: Path, script: list[list[dict]]):
    """A started session whose model plays ``script``: one response per
    entry, each a list of tool calls; an empty list ends the turn."""
    from loom.core import session as session_module
    from loom.core.cognition.providers import LLMResponse, ToolUse
    from loom.core.harness.registry import ToolDefinition
    from loom.core.session import LoomSession

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(session_module, "build_router", lambda *a, **k: MagicMock())
    monkeypatch.setattr(session_module, "_load_loom_config", lambda: {})
    monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {})
    monkeypatch.setattr(session_module, "build_embedding_provider", lambda env, cfg: None)
    from rich.prompt import Confirm

    monkeypatch.setattr(Confirm, "ask", lambda *a, **k: True)

    session = LoomSession(model="gpt-test", db_path=str(tmp_path / "loom.db"), workspace=workspace)
    await session.start()

    runs: list[dict] = []

    async def post(call: ToolCall) -> ToolResult:
        runs.append(dict(call.args))
        return ToolResult(call.id, call.tool_name, False, error="Forum channel 0 not found: 404 Unknown Channel")

    session.registry.register(ToolDefinition(
        name="post", description="post", trust_level=TrustLevel.SAFE,
        input_schema={"type": "object", "properties": {}}, executor=post,
    ))

    steps = iter(script)
    counter = {"n": 0}

    async def stream_chat(**kwargs):
        calls = next(steps, [])
        uses = []
        for args in calls:
            counter["n"] += 1
            uses.append(ToolUse(id=f"call-{counter['n']}", name="post", args=args))
        yield "", LLMResponse(
            text=None if uses else "done",
            tool_uses=uses,
            stop_reason="tool_use" if uses else "end_turn",
            input_tokens=10, output_tokens=5,
            raw_message={
                "role": "assistant", "content": None if uses else "done",
                **({"tool_calls": [
                    {"id": u.id, "type": "function",
                     "function": {"name": u.name, "arguments": "{}"}} for u in uses
                ]} if uses else {}),
            },
        )

    session.router = SimpleNamespace(
        stream_chat=stream_chat,
        native_max_tokens=lambda model: None,
        format_tool_result=lambda model, tid, content, success=True: {
            "role": "tool", "tool_call_id": tid, "content": content,
        },
    )
    session._judge_mode = "off"
    return session, runs


def _tool_contents(session) -> list[str]:
    return [m["content"] for m in session.messages if m.get("role") == "tool"]


async def test_sequential_repeats_are_nudged_but_every_call_still_runs(monkeypatch, tmp_path):
    session, runs = await _session(monkeypatch, tmp_path, [[{"id": 0}]] * 4 + [[]])
    try:
        async for _ in session.stream_turn("post the briefing"):
            pass
        contents = _tool_contents(session)
        assert len(runs) == 4  # never blocked
        assert [NUDGE in c for c in contents] == [False, False, True, True]
    finally:
        await session.stop()


async def test_parallel_batch_of_identical_calls_is_nudged(monkeypatch, tmp_path):
    session, runs = await _session(monkeypatch, tmp_path, [[{"id": 0}] * 3, []])
    try:
        async for _ in session.stream_turn("post the briefing"):
            pass
        assert len(runs) == 3
        assert [NUDGE in c for c in _tool_contents(session)] == [False, False, True]
    finally:
        await session.stop()


async def test_count_starts_over_on_the_next_turn(monkeypatch, tmp_path):
    session, _ = await _session(
        monkeypatch, tmp_path,
        [[{"id": 0}]] * 2 + [[]] + [[{"id": 0}]] + [[]],
    )
    try:
        async for _ in session.stream_turn("first"):
            pass
        async for _ in session.stream_turn("second"):
            pass
        assert not any(NUDGE in c for c in _tool_contents(session))
    finally:
        await session.stop()


async def test_identical_call_loop_is_ended_at_the_hard_limit(monkeypatch, tmp_path):
    """The 2026-10-29 shape: the model would keep re-posting forever."""
    from loom.core.events import TurnDone, TurnDropped

    session, runs = await _session(monkeypatch, tmp_path, [[{"id": 0}]] * 400)
    try:
        events = [e async for e in session.stream_turn("post the briefing")]
        assert len(runs) == HARD_LIMIT
        drops = [e for e in events if isinstance(e, TurnDropped)]
        assert [d.stop_reason for d in drops] == ["repeat_call_limit"]
        assert "post" in drops[0].provider_error_detail
        done = [e for e in events if isinstance(e, TurnDone)]
        assert done and done[-1].stop_reason == "repeat_call_limit"
        assert "ending this turn" in _tool_contents(session)[-1]
    finally:
        await session.stop()


async def test_hard_limit_mid_batch_still_answers_every_call(monkeypatch, tmp_path):
    """Stopping mid-batch would orphan tool_calls; the batch finishes first."""
    session, runs = await _session(
        monkeypatch, tmp_path,
        [[{"id": 0}]] * (HARD_LIMIT - 2) + [[{"id": 0}] * 4] + [[{"id": 0}]] * 10,
    )
    try:
        async for _ in session.stream_turn("post the briefing"):
            pass
        assert len(runs) == HARD_LIMIT + 2
        call_ids = [
            tc["id"] for m in session.messages if m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or [])
        ]
        answered = {m["tool_call_id"] for m in session.messages if m.get("role") == "tool"}
        assert set(call_ids) == answered
    finally:
        await session.stop()


async def test_hard_limit_does_not_carry_into_the_next_turn(monkeypatch, tmp_path):
    from loom.core.events import TurnDone

    session, runs = await _session(
        monkeypatch, tmp_path,
        [[{"id": 0}]] * HARD_LIMIT + [[{"id": 0}], []],
    )
    try:
        async for _ in session.stream_turn("first"):
            pass
        events = [e async for e in session.stream_turn("second")]
        assert len(runs) == HARD_LIMIT + 1
        assert [e.stop_reason for e in events if isinstance(e, TurnDone)] == ["complete"]
    finally:
        await session.stop()
