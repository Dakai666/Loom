"""
Tests for JITRetrievalMiddleware (Issue #197 Phase 1).

The middleware spills large tool outputs to scratchpad and replaces the
inline output with a structured placeholder. These tests cover the
threshold logic, opt-out via inline_only, async-mode passthrough,
graceful degradation on scratchpad failures, and placeholder shape.
"""

from __future__ import annotations

import pytest

from loom.core.harness.middleware import (
    JITRetrievalMiddleware,
    MiddlewarePipeline,
    ToolCall,
    ToolResult,
)
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.jobs.scratchpad import Scratchpad


def _call(tool_name: str = "fetch_url") -> ToolCall:
    return ToolCall(
        id="c1", tool_name=tool_name, args={},
        trust_level=TrustLevel.SAFE, session_id="s",
    )


def _make_registry(*tools: ToolDefinition) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


def _make_tool(name: str, *, inline_only: bool = False) -> ToolDefinition:
    async def _executor(call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True, output="placeholder — overridden in tests",
        )
    return ToolDefinition(
        name=name, description="", input_schema={},
        executor=_executor, trust_level=TrustLevel.SAFE,
        inline_only=inline_only,
    )


async def _run(jit: JITRetrievalMiddleware, call: ToolCall, output: str = "",
               *, success: bool = True, metadata: dict | None = None) -> ToolResult:
    """Drive jit.process with an inner handler returning a fixed result."""
    async def _inner(c: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=c.id, tool_name=c.tool_name,
            success=success, output=output, metadata=metadata or {},
        )
    return await jit.process(call, _inner)


class TestJITRetrievalThreshold:
    """Threshold logic — spill above, inline below."""

    async def test_output_below_threshold_stays_inline(self) -> None:
        scratchpad = Scratchpad()
        registry = _make_registry(_make_tool("fetch_url"))
        jit = JITRetrievalMiddleware(scratchpad, registry, threshold_chars=100)

        result = await _run(jit, _call("fetch_url"), output="x" * 50)

        assert result.output == "x" * 50
        assert "jit_spilled" not in result.metadata
        assert scratchpad.list_refs() == []

    async def test_output_above_threshold_spills_to_scratchpad(self) -> None:
        scratchpad = Scratchpad()
        registry = _make_registry(_make_tool("fetch_url"))
        jit = JITRetrievalMiddleware(scratchpad, registry, threshold_chars=100)

        big_output = "y" * 5000
        result = await _run(jit, _call("fetch_url"), output=big_output)

        # Inline output is the placeholder, not the original content
        assert "spilled to scratchpad" in result.output
        assert "fetch_url" in result.output
        assert "5000 chars" in result.output

        # Scratchpad has the original content under the metadata's ref
        assert result.metadata["jit_spilled"] is True
        ref = result.metadata["jit_ref"]
        assert ref.startswith("auto_fetch_url_")
        assert scratchpad.read(ref) == big_output

    async def test_threshold_at_exact_boundary(self) -> None:
        """Output equal to threshold stays inline (>, not >=)."""
        scratchpad = Scratchpad()
        registry = _make_registry(_make_tool("fetch_url"))
        jit = JITRetrievalMiddleware(scratchpad, registry, threshold_chars=100)

        result = await _run(jit, _call("fetch_url"), output="z" * 100)

        assert result.output == "z" * 100
        assert "jit_spilled" not in result.metadata

    async def test_threshold_zero_disables_spilling(self) -> None:
        scratchpad = Scratchpad()
        registry = _make_registry(_make_tool("fetch_url"))
        jit = JITRetrievalMiddleware(scratchpad, registry, threshold_chars=0)

        big_output = "y" * 100_000
        result = await _run(jit, _call("fetch_url"), output=big_output)

        assert result.output == big_output
        assert scratchpad.list_refs() == []


class TestJITInlineOnlyOptOut:
    """Tools marked inline_only=True bypass JIT regardless of size."""

    async def test_inline_only_tool_never_spills(self) -> None:
        scratchpad = Scratchpad()
        registry = _make_registry(_make_tool("task_read", inline_only=True))
        jit = JITRetrievalMiddleware(scratchpad, registry, threshold_chars=100)

        big_output = "important content " * 1000  # well past threshold
        result = await _run(jit, _call("task_read"), output=big_output)

        assert result.output == big_output
        assert "jit_spilled" not in result.metadata
        assert scratchpad.list_refs() == []

    async def test_unknown_tool_still_spills(self) -> None:
        """If the tool isn't in the registry, default to spilling — better
        to spill an unknown than silently bloat context."""
        scratchpad = Scratchpad()
        registry = _make_registry()  # empty
        jit = JITRetrievalMiddleware(scratchpad, registry, threshold_chars=100)

        big_output = "?" * 5000
        result = await _run(jit, _call("mystery_tool"), output=big_output)

        assert result.metadata["jit_spilled"] is True


class TestJITAsyncModePassthrough:
    """Tools that already returned a job_id (async_mode=True) should not be
    re-spilled — the body is destined for scratchpad through its own path."""

    async def test_async_mode_result_skips_spill(self) -> None:
        scratchpad = Scratchpad()
        registry = _make_registry(_make_tool("fetch_url"))
        jit = JITRetrievalMiddleware(scratchpad, registry, threshold_chars=100)

        # Job submission output is short anyway, but use a long one to
        # prove the metadata flag — not size — is what skips JIT.
        long_msg = "Submitted as job_xyz. " * 100
        result = await _run(
            jit, _call("fetch_url"), output=long_msg,
            metadata={"job_id": "job_xyz", "async": True},
        )

        assert result.output == long_msg
        assert "jit_spilled" not in result.metadata
        assert result.metadata.get("async") is True


class TestJITFailureBehavior:
    """Failed tool results: spill on size, like successful ones (consistency)."""

    async def test_failed_result_with_large_error_output_spills(self) -> None:
        scratchpad = Scratchpad()
        registry = _make_registry(_make_tool("run_bash"))
        jit = JITRetrievalMiddleware(scratchpad, registry, threshold_chars=100)

        big_error_output = "stderr line\n" * 1000
        result = await _run(
            jit, _call("run_bash"), output=big_error_output, success=False,
        )

        assert result.success is False
        assert "spilled to scratchpad" in result.output
        assert result.metadata["jit_spilled"] is True

    async def test_scratchpad_write_failure_keeps_inline(self) -> None:
        """Graceful degradation: if scratchpad.write raises, log a warning
        and return the original result unchanged. JIT must never fail the
        underlying tool call."""

        class _FailingScratchpad:
            def write(self, ref, content):
                raise RuntimeError("disk full")

            def read(self, ref):
                raise KeyError(ref)

            def list_refs(self):
                return []

        registry = _make_registry(_make_tool("fetch_url"))
        jit = JITRetrievalMiddleware(_FailingScratchpad(), registry, threshold_chars=100)

        big_output = "y" * 5000
        result = await _run(jit, _call("fetch_url"), output=big_output)

        # Original content preserved; no spill metadata claims
        assert result.output == big_output
        assert "jit_spilled" not in result.metadata


class TestJITPlaceholderShape:
    """Placeholder text is the agent's only signal of what was spilled —
    its structure must communicate retrieval paths clearly."""

    async def test_placeholder_contains_tool_name_size_and_ref(self) -> None:
        scratchpad = Scratchpad()
        registry = _make_registry(_make_tool("fetch_url"))
        jit = JITRetrievalMiddleware(scratchpad, registry, threshold_chars=100)

        result = await _run(jit, _call("fetch_url"), output="z" * 5000)

        placeholder = result.output
        ref = result.metadata["jit_ref"]
        assert "fetch_url" in placeholder
        assert ref in placeholder
        assert "5000 chars" in placeholder

    async def test_placeholder_offers_both_retrieval_paths(self) -> None:
        """Cached (scratchpad_read) AND fresh (re-call tool) — agent should
        understand both options without parsing prose."""
        scratchpad = Scratchpad()
        registry = _make_registry(_make_tool("fetch_url"))
        jit = JITRetrievalMiddleware(scratchpad, registry, threshold_chars=100)

        result = await _run(jit, _call("fetch_url"), output="z" * 5000)

        placeholder = result.output
        assert "scratchpad_read" in placeholder
        assert "fresh" in placeholder.lower()  # re-call hint

    async def test_metadata_carries_structured_jit_info(self) -> None:
        """Telemetry / future learning loops parse metadata, not the
        placeholder text — pin the metadata shape."""
        scratchpad = Scratchpad()
        registry = _make_registry(_make_tool("fetch_url"))
        jit = JITRetrievalMiddleware(scratchpad, registry, threshold_chars=100)

        result = await _run(jit, _call("fetch_url"), output="z" * 5000)

        meta = result.metadata
        assert meta["jit_spilled"] is True
        assert meta["jit_ref"].startswith("auto_fetch_url_")
        assert meta["jit_original_size"] == 5000


class TestJITRefUniqueness:
    """Many spills in a turn must not collide on ref names."""

    async def test_multiple_spills_get_distinct_refs(self) -> None:
        scratchpad = Scratchpad()
        registry = _make_registry(_make_tool("fetch_url"))
        jit = JITRetrievalMiddleware(scratchpad, registry, threshold_chars=100)

        refs = set()
        for i in range(10):
            result = await _run(
                jit, _call("fetch_url"), output=f"content {i} " + "x" * 200,
            )
            refs.add(result.metadata["jit_ref"])

        assert len(refs) == 10
        assert len(scratchpad.list_refs()) == 10
