"""#334 — emit deferred event types.

Covers ``thought`` / ``model_event`` / ``judge_verdict`` / ``artifact_emit``
landing in the ledger. Session-level helpers are exercised against a stub
session that owns just enough state (emitter, store, _turn_* aggregators)
to drive the helper without booting the full LoomSession machinery.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest_asyncio

from loom.core.harness.middleware import (
    LifecycleMiddleware,
    MiddlewarePipeline,
    ToolCall,
    ToolResult,
)
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.ledger import (
    LedgerEmitter,
    LedgerStore,
    async_correlation_scope,
    async_turn_scope,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def ledger(tmp_path: Path) -> LedgerStore:
    s = LedgerStore(
        db_path=tmp_path / "ledger.db",
        blob_dir=tmp_path / "blobs",
    )
    await s.open()
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
def emitter(ledger: LedgerStore) -> LedgerEmitter:
    return LedgerEmitter(ledger, session_id="sess_def")


def _make_call(
    name: str = "write_file", args: dict | None = None
) -> ToolCall:
    return ToolCall(
        tool_name=name,
        args=args or {},
        trust_level=TrustLevel.SAFE,
        session_id="sess_def",
    )


def _make_session_stub(emitter: LedgerEmitter, store: LedgerStore):
    """Bind the four session helpers from #334 onto an Any-shaped stub.

    Avoids spinning up a full LoomSession (which requires Discord, memory,
    Anthropic credentials etc.). The helpers only touch ``_ledger_emitter``,
    ``_ledger_store`` and the ``_turn_*`` aggregators.
    """
    from loom.core.session import LoomSession

    class _Stub:
        pass

    s = _Stub()
    s._ledger_emitter = emitter
    s._ledger_store = store
    s._turn_token_usage = {}
    s._turn_thought_text = ""
    s._turn_thought_started = 0.0
    s._turn_tool_calls_in_turn = 0
    s._turn_judge_capture_signal = False
    s._turn_artifact_max_size = 0

    # Tier helper used by _emit_ledger_model_event.
    s._active_tier = lambda: 1  # type: ignore[attr-defined]

    s._emit_ledger_model_event = LoomSession._emit_ledger_model_event.__get__(s)
    s._emit_ledger_judge_verdict = LoomSession._emit_ledger_judge_verdict.__get__(s)
    s._commit_or_discard_thought = LoomSession._commit_or_discard_thought.__get__(s)
    s._emit_ledger_turn_end = LoomSession._emit_ledger_turn_end.__get__(s)
    return s


class _FakeResp:
    def __init__(
        self, *, input_tokens: int, output_tokens: int,
        cache_read: int = 0, cache_creation: int = 0,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = cache_creation


# ---------------------------------------------------------------------------
# model_event
# ---------------------------------------------------------------------------


async def test_model_event_emits_and_aggregates(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    s = _make_session_stub(emitter, ledger)
    async with async_turn_scope("turn_me"), async_correlation_scope("c1"):
        await s._emit_ledger_model_event("claude-x", _FakeResp(
            input_tokens=100, output_tokens=20, cache_read=8,
        ))
        await s._emit_ledger_model_event("claude-x", _FakeResp(
            input_tokens=110, output_tokens=30,
        ))
    rows = [r for r in await ledger.fetch_by_turn("turn_me")
            if r.event_type == "model_event"]
    assert len(rows) == 2
    assert rows[0].payload["model"] == "claude-x"
    assert rows[0].payload["tier"] == 1
    # Second call's usage is independent on the event but accumulator sums.
    assert s._turn_token_usage["input_tokens"] == 210
    assert s._turn_token_usage["output_tokens"] == 50
    assert s._turn_token_usage["cache_read_input_tokens"] == 8


async def test_turn_end_carries_aggregated_token_usage(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    s = _make_session_stub(emitter, ledger)
    async with async_turn_scope("turn_te"), async_correlation_scope("c1"):
        await s._emit_ledger_model_event("m", _FakeResp(
            input_tokens=42, output_tokens=7,
        ))
        await s._emit_ledger_turn_end("turn_te", "clean", 1234)
    rows = [r for r in await ledger.fetch_by_turn("turn_te")
            if r.event_type == "turn_end"]
    assert len(rows) == 1
    assert rows[0].payload["token_usage"]["input_tokens"] == 42
    assert rows[0].payload["token_usage"]["output_tokens"] == 7
    assert rows[0].payload["outcome"] == "clean"


# ---------------------------------------------------------------------------
# judge_verdict
# ---------------------------------------------------------------------------


async def test_judge_verdict_pass_emits_and_no_capture_signal(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    from loom.core.cognition.judge import JudgeVerdict
    s = _make_session_stub(emitter, ledger)
    async with async_turn_scope("turn_j"), async_correlation_scope("c1"):
        await s._emit_ledger_judge_verdict(
            JudgeVerdict(verdict="pass", reason="ok"), "turn_final_text"
        )
    rows = [r for r in await ledger.fetch_by_turn("turn_j")
            if r.event_type == "judge_verdict"]
    assert len(rows) == 1
    assert rows[0].payload["verdict"] == "PASS"
    assert s._turn_judge_capture_signal is False


async def test_judge_verdict_fail_sets_capture_signal(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    from loom.core.cognition.judge import JudgeVerdict
    s = _make_session_stub(emitter, ledger)
    async with async_turn_scope("turn_jf"), async_correlation_scope("c1"):
        await s._emit_ledger_judge_verdict(
            JudgeVerdict(verdict="fail", reason="bad"), "turn_final_text"
        )
    rows = [r for r in await ledger.fetch_by_turn("turn_jf")
            if r.event_type == "judge_verdict"]
    assert rows[0].payload["verdict"] == "FAIL"
    assert s._turn_judge_capture_signal is True


async def test_judge_verdict_error_maps_to_error_literal(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    from loom.core.cognition.judge import JudgeVerdict
    s = _make_session_stub(emitter, ledger)
    async with async_turn_scope("turn_je"), async_correlation_scope("c1"):
        await s._emit_ledger_judge_verdict(
            JudgeVerdict(
                verdict="uncertain",
                reason="judge call failed",
                error="TimeoutError",
            ),
            "turn_final_text",
        )
    rows = [r for r in await ledger.fetch_by_turn("turn_je")
            if r.event_type == "judge_verdict"]
    assert rows[0].payload["verdict"] == "ERROR"


# ---------------------------------------------------------------------------
# thought (commit-or-discard)
# ---------------------------------------------------------------------------


async def test_thought_discarded_on_clean_turn_no_signal(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    s = _make_session_stub(emitter, ledger)
    s._turn_thought_text = "some reasoning that nobody needs to keep"
    async with async_turn_scope("turn_th_drop"), async_correlation_scope("c1"):
        await s._commit_or_discard_thought("turn_th_drop", "clean")
    rows = [r for r in await ledger.fetch_by_turn("turn_th_drop")
            if r.event_type == "thought"]
    assert rows == []


async def test_thought_committed_when_judge_fail_signal(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    s = _make_session_stub(emitter, ledger)
    s._turn_thought_text = "wrestled with prompt; eventually misfired"
    s._turn_judge_capture_signal = True
    s._turn_tool_calls_in_turn = 3
    async with async_turn_scope("turn_th_keep"), async_correlation_scope("c1"):
        await s._commit_or_discard_thought("turn_th_keep", "clean")
    rows = [r for r in await ledger.fetch_by_turn("turn_th_keep")
            if r.event_type == "thought"]
    assert len(rows) == 1
    p = rows[0].payload
    assert p["full_text"].startswith("wrestled")
    assert p["external_ref"] is None  # well under 50 KB
    assert p["produced_tool_calls"] == 3
    assert p["digest"].startswith("sha256:")


async def test_thought_committed_on_large_artifact_signal(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    """Review B1 regression guard — `artifact > 10 KB` must trigger
    thought capture (doc/53 §3.3)."""
    s = _make_session_stub(emitter, ledger)
    s._turn_thought_text = "produced a sizeable file"
    s._turn_artifact_max_size = 12_345  # > 10 KB threshold
    async with async_turn_scope("turn_th_artifact"), async_correlation_scope("c1"):
        await s._commit_or_discard_thought("turn_th_artifact", "clean")
    rows = [r for r in await ledger.fetch_by_turn("turn_th_artifact")
            if r.event_type == "thought"]
    assert len(rows) == 1


async def test_extract_artifact_info_dispatch_known_and_unknown(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    """Review S1 — single dict-dispatch extractor used by both
    middleware emit and session size accumulator."""
    from loom.core.harness.middleware import extract_artifact_info
    ok = ToolResult(
        call_id="c1", tool_name="write_file", success=True,
        output="ok", metadata={"_resolved_path": "/x.txt"},
    )
    fail = ToolResult(
        call_id="c2", tool_name="write_file", success=False, error="boom",
    )
    info = extract_artifact_info(
        _make_call("write_file", {"path": "/x.txt", "content": "hi"}), ok,
    )
    assert info is not None
    assert info["artifact_type"] == "text_file"
    assert info["size_bytes"] == 2
    assert extract_artifact_info(
        _make_call("write_file", {"path": "/x.txt", "content": "hi"}), fail,
    ) is None
    assert extract_artifact_info(
        _make_call("read_file", {"path": "/x.txt"}), ok,
    ) is None


async def test_thought_committed_on_abandoned_outcome(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    s = _make_session_stub(emitter, ledger)
    s._turn_thought_text = "user pressed cancel mid-thought"
    async with async_turn_scope("turn_th_abandon"), async_correlation_scope("c1"):
        await s._commit_or_discard_thought("turn_th_abandon", "abandoned")
    rows = [r for r in await ledger.fetch_by_turn("turn_th_abandon")
            if r.event_type == "thought"]
    assert len(rows) == 1


async def test_thought_blob_spillover_above_threshold(
    ledger: LedgerStore, emitter: LedgerEmitter, tmp_path: Path
) -> None:
    s = _make_session_stub(emitter, ledger)
    s._turn_thought_text = "x" * 60_000  # > 50 KB threshold
    s._turn_judge_capture_signal = True
    async with async_turn_scope("turn_th_blob"), async_correlation_scope("c1"):
        await s._commit_or_discard_thought("turn_th_blob", "clean")
    rows = [r for r in await ledger.fetch_by_turn("turn_th_blob")
            if r.event_type == "thought"]
    assert len(rows) == 1
    p = rows[0].payload
    assert p["full_text"] is None
    assert p["external_ref"] and p["external_ref"].startswith("turn_th_blob/")
    assert (ledger.blob_dir / p["external_ref"]).exists()


# ---------------------------------------------------------------------------
# artifact_emit (driven through LifecycleMiddleware on real producer names)
# ---------------------------------------------------------------------------


def _producer_registry(name: str, output: str, metadata: dict | None = None):
    reg = ToolRegistry()

    async def handler(call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.id,
            tool_name=call.tool_name,
            success=True,
            output=output,
            metadata=metadata or {},
        )

    reg.register(
        ToolDefinition(
            name=name,
            description="artifact producer",
            trust_level=TrustLevel.SAFE,
            input_schema={"type": "object"},
            executor=handler,
        )
    )
    return reg


async def test_artifact_emit_for_write_file(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    reg = _producer_registry(
        "write_file", "Written 11 chars to /tmp/out.txt",
        metadata={"_resolved_path": "/tmp/out.txt"},
    )
    pipeline = MiddlewarePipeline(
        [LifecycleMiddleware(registry=reg, ledger_emitter=emitter)]
    )
    call = _make_call("write_file", {"path": "/tmp/out.txt", "content": "hello world"})
    async with async_turn_scope("turn_aw"), async_correlation_scope("c1"):
        await pipeline.execute(call, reg.get("write_file").executor)

    rows = [r for r in await ledger.fetch_by_turn("turn_aw")
            if r.event_type == "artifact_emit"]
    assert len(rows) == 1
    p = rows[0].payload
    assert p["artifact_type"] == "text_file"
    assert p["size_bytes"] == len("hello world".encode("utf-8"))
    assert p["location"] == "/tmp/out.txt"
    assert p["digest"].startswith("sha256:")


async def test_artifact_emit_for_openai_text_to_image(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    import json
    reg = _producer_registry(
        "openai__text_to_image",
        json.dumps({
            "path": "img/out.png", "model": "gpt-image-2",
            "credential_source": "env", "bytes": 4096,
        }),
    )
    pipeline = MiddlewarePipeline(
        [LifecycleMiddleware(registry=reg, ledger_emitter=emitter)]
    )
    call = _make_call(
        "openai__text_to_image", {"prompt": "a cat", "output_path": "img/out.png"},
    )
    async with async_turn_scope("turn_ai"), async_correlation_scope("c1"):
        await pipeline.execute(call, reg.get("openai__text_to_image").executor)

    rows = [r for r in await ledger.fetch_by_turn("turn_ai")
            if r.event_type == "artifact_emit"]
    assert len(rows) == 1
    p = rows[0].payload
    assert p["artifact_type"] == "image"
    assert p["size_bytes"] == 4096
    assert p["location"] == "img/out.png"


async def test_artifact_emit_skipped_on_failed_tool(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    reg = ToolRegistry()

    async def handler(call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=False, error="boom",
        )

    reg.register(
        ToolDefinition(
            name="write_file", description="x", trust_level=TrustLevel.SAFE,
            input_schema={"type": "object"}, executor=handler,
        )
    )
    pipeline = MiddlewarePipeline(
        [LifecycleMiddleware(registry=reg, ledger_emitter=emitter)]
    )
    call = _make_call("write_file", {"path": "x", "content": "y"})
    async with async_turn_scope("turn_afail"), async_correlation_scope("c1"):
        await pipeline.execute(call, reg.get("write_file").executor)

    rows = [r for r in await ledger.fetch_by_turn("turn_afail")
            if r.event_type == "artifact_emit"]
    assert rows == []


async def test_artifact_emit_skipped_for_non_producer_tool(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    reg = _producer_registry("read_file", "file contents")
    pipeline = MiddlewarePipeline(
        [LifecycleMiddleware(registry=reg, ledger_emitter=emitter)]
    )
    call = _make_call("read_file", {"path": "x"})
    async with async_turn_scope("turn_anp"), async_correlation_scope("c1"):
        await pipeline.execute(call, reg.get("read_file").executor)

    rows = [r for r in await ledger.fetch_by_turn("turn_anp")
            if r.event_type == "artifact_emit"]
    assert rows == []
