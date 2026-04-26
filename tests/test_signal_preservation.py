"""
Issue #212: framework signal preservation.

These tests pin the contract that any place where the harness used to
swallow a signal (tool exception, middleware decision, mutator LLM
failure) now produces structured, queryable output.

Scope of this file matches the three actionable items left in #212
after #196/#197/#205 closed the rest:

* ``ToolResult.error_context`` — full traceback for tool/middleware
  exceptions, separate from the single-line ``error`` shown to the agent.
* ``call.metadata["middleware_trace"]`` — append-only list of
  ``{middleware, decision, …}`` entries so a denied/raised result can be
  attributed to the middleware that produced the verdict.
* ``SkillMutator.last_failure`` — non-None record of the most recent
  silent LLM failure so the skill-evolution loop is no longer invisible.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from loom.core.harness.lifecycle import ActionState
from loom.core.harness.middleware import (
    BlastRadiusMiddleware,
    LifecycleGateMiddleware,
    LifecycleMiddleware,
    MiddlewarePipeline,
    ToolCall,
    ToolResult,
)
from loom.core.harness.permissions import PermissionContext, TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.harness.validation import SchemaValidationMiddleware


# ---------------------------------------------------------------------------
# Helpers — pared-down versions of the test_lifecycle.py fixtures
# ---------------------------------------------------------------------------

def _registry(*tools: ToolDefinition) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


def _call(tool_name: str, trust_level: TrustLevel = TrustLevel.SAFE) -> ToolCall:
    return ToolCall(
        tool_name=tool_name,
        args={},
        trust_level=trust_level,
        session_id="t",
    )


async def _ok(call: ToolCall) -> ToolResult:
    return ToolResult(
        call_id=call.id, tool_name=call.tool_name,
        success=True, output="ok",
    )


def _pipeline(registry: ToolRegistry, perm: PermissionContext | None = None) -> MiddlewarePipeline:
    perm = perm or PermissionContext(session_id="t")
    return MiddlewarePipeline([
        LifecycleMiddleware(registry=registry),
        SchemaValidationMiddleware(registry=registry),
        BlastRadiusMiddleware(perm_ctx=perm, confirm_fn=AsyncMock(return_value=True)),
        LifecycleGateMiddleware(registry=registry),
    ])


# ---------------------------------------------------------------------------
# #2 — ToolResult.error_context
# ---------------------------------------------------------------------------

class TestToolResultErrorContext:
    def test_default_is_none(self) -> None:
        """Successful results carry no error_context."""
        r = ToolResult(call_id="c", tool_name="t", success=True, output="x")
        assert r.error_context is None

    @pytest.mark.asyncio
    async def test_tool_raise_populates_error_context(self) -> None:
        """When a tool handler raises, the harness must surface the full
        traceback in ``error_context`` (not just ``str(exc)`` in ``error``)."""
        async def boom(call: ToolCall) -> ToolResult:
            raise RuntimeError("kaboom from tool")

        tool = ToolDefinition(
            name="bomb", description="",
            input_schema={}, executor=boom,
            trust_level=TrustLevel.SAFE,
        )
        result = await _pipeline(_registry(tool)).execute(_call("bomb"), boom)

        assert not result.success
        assert result.failure_type == "execution_error"
        # The user-facing ``error`` stays compact …
        assert result.error == "kaboom from tool"
        # … while ``error_context`` carries the full traceback so
        # forensics / telemetry can reconstruct the call site.
        assert result.error_context is not None
        assert "Traceback" in result.error_context
        assert "kaboom from tool" in result.error_context
        # The frame for ``boom`` itself must show up so debugging is real.
        assert "boom" in result.error_context

    @pytest.mark.asyncio
    async def test_error_context_survives_jit_spill(self) -> None:
        """JIT spilling rewrites ``output`` but must not drop
        ``error_context`` from the wrapped result."""
        from loom.core.harness.middleware import JITRetrievalMiddleware

        class _Pad:
            def __init__(self) -> None:
                self.store: dict[str, str] = {}
            def write(self, ref: str, value: str) -> None:
                self.store[ref] = value

        pad = _Pad()
        jit = JITRetrievalMiddleware(
            scratchpad=pad, registry=ToolRegistry(), threshold_chars=10,
        )

        # Use a failure ToolResult with a large output AND error_context
        # to verify JIT preserves the latter.
        big_out = "x" * 200
        async def handler(_: ToolCall) -> ToolResult:
            return ToolResult(
                call_id="c", tool_name="t", success=False,
                output=big_out, error="failed",
                error_context="Traceback (...): ...",
            )

        pipeline = MiddlewarePipeline([jit])
        result = await pipeline.execute(_call("t"), handler)

        # JIT replaced ``output`` with a placeholder …
        assert result.output != big_out
        # … but error_context survived intact.
        assert result.error_context == "Traceback (...): ..."


# ---------------------------------------------------------------------------
# #6 — middleware_trace
# ---------------------------------------------------------------------------

class TestMiddlewareTrace:
    @pytest.mark.asyncio
    async def test_authorize_recorded(self) -> None:
        """A pre-authorized SAFE tool produces a BlastRadius authorize entry."""
        tool = ToolDefinition(
            name="ok", description="",
            input_schema={}, executor=_ok,
            trust_level=TrustLevel.SAFE,
        )
        call = _call("ok")
        await _pipeline(_registry(tool)).execute(call, _ok)

        trace = call.metadata.get("middleware_trace", [])
        decisions = [(e["middleware"], e["decision"]) for e in trace]
        assert ("BlastRadius", "authorize") in decisions

    @pytest.mark.asyncio
    async def test_tool_raised_recorded(self) -> None:
        """A handler that raises must show up in middleware_trace as
        ``LifecycleGate / tool-raised`` with reason + error_context."""
        async def boom(call: ToolCall) -> ToolResult:
            raise ValueError("trace-me")

        tool = ToolDefinition(
            name="bomb", description="",
            input_schema={}, executor=boom,
            trust_level=TrustLevel.SAFE,
        )
        call = _call("bomb")
        await _pipeline(_registry(tool)).execute(call, boom)

        trace = call.metadata.get("middleware_trace", [])
        raised = [
            e for e in trace
            if e["middleware"] == "LifecycleGate"
            and e["decision"] == "tool-raised"
        ]
        assert len(raised) == 1
        assert raised[0]["reason"] == "trace-me"
        assert "Traceback" in raised[0]["error_context"]

    @pytest.mark.asyncio
    async def test_precondition_failure_recorded(self) -> None:
        """A failing precondition produces a ``precondition-failed`` trace
        entry that names which check (by index) failed."""
        async def always_false(_: ToolCall) -> bool:
            return False

        tool = ToolDefinition(
            name="gated", description="",
            input_schema={}, executor=_ok,
            trust_level=TrustLevel.SAFE,
            preconditions=["must be true"],
            precondition_checks=[always_false],
        )
        call = _call("gated")
        await _pipeline(_registry(tool)).execute(call, _ok)

        trace = call.metadata.get("middleware_trace", [])
        fails = [
            e for e in trace
            if e["middleware"] == "LifecycleGate"
            and e["decision"] == "precondition-failed"
        ]
        assert len(fails) == 1
        assert fails[0]["check_index"] == 0
        assert fails[0]["reason"] == "must be true"

    @pytest.mark.asyncio
    async def test_trace_is_ordered(self) -> None:
        """Trace order matches execution order: BlastRadius authorizes
        before LifecycleGate fires the tool."""
        async def boom(call: ToolCall) -> ToolResult:
            raise RuntimeError("x")

        tool = ToolDefinition(
            name="bomb", description="",
            input_schema={}, executor=boom,
            trust_level=TrustLevel.SAFE,
        )
        call = _call("bomb")
        await _pipeline(_registry(tool)).execute(call, boom)

        trace = call.metadata.get("middleware_trace", [])
        decisions = [(e["middleware"], e["decision"]) for e in trace]
        # Authorize must precede tool-raised.
        i_auth = decisions.index(("BlastRadius", "authorize"))
        i_raise = decisions.index(("LifecycleGate", "tool-raised"))
        assert i_auth < i_raise


# ---------------------------------------------------------------------------
# #4 — SkillMutator.last_failure
# ---------------------------------------------------------------------------

class TestSkillMutatorLastFailure:
    def test_default_is_none(self) -> None:
        from loom.core.cognition.skill_mutator import SkillMutator
        m = SkillMutator(router=AsyncMock(), model="x", enabled=True)
        assert m.last_failure is None

    @pytest.mark.asyncio
    async def test_propose_candidate_records_llm_failure(self) -> None:
        """When the LLM call raises inside ``propose_candidate`` the mutator
        must (a) still return ``None`` (non-fatal contract preserved) and
        (b) populate ``last_failure`` so the session can see the silence."""
        from loom.core.cognition.skill_mutator import SkillMutator

        router = AsyncMock()
        router.chat.side_effect = RuntimeError("provider 503")

        m = SkillMutator(router=router, model="x", enabled=True, min_suggestions=1)

        parent = SimpleNamespace(name="test-skill", body="# Skill\nbody text", version="v1")
        diagnostic = SimpleNamespace(
            mutation_suggestions=["do X"],
            instructions_violated=[],
            failure_patterns=[],
            quality_score=2.0,
        )

        result = await m.propose_candidate(parent, diagnostic, session_id="s")

        assert result is None
        assert m.last_failure is not None
        assert m.last_failure["path"] == "propose_candidate"
        assert m.last_failure["skill"] == "test-skill"
        assert m.last_failure["error_type"] == "RuntimeError"
        assert "503" in m.last_failure["error"]
        assert isinstance(m.last_failure["timestamp"], float)

    @pytest.mark.asyncio
    async def test_from_batch_diagnostic_records_llm_failure(self) -> None:
        """Same contract for the batch path."""
        from loom.core.cognition.skill_mutator import SkillMutator

        router = AsyncMock()
        router.chat.side_effect = TimeoutError("router timeout")

        m = SkillMutator(router=router, model="x", enabled=True)

        parent = SimpleNamespace(name="batch-skill", body="# Skill\nbody", version="v1")
        batch = SimpleNamespace(
            aggregated_suggestions=["s"],
            aggregated_violations=[],
            aggregated_failures=[],
            improvement=0.05,
            diagnostics=[],
        )

        result = await m.from_batch_diagnostic(parent, batch, session_id="s")

        assert result is None
        assert m.last_failure is not None
        assert m.last_failure["path"] == "from_batch_diagnostic"
        assert m.last_failure["skill"] == "batch-skill"
        assert m.last_failure["error_type"] == "TimeoutError"
