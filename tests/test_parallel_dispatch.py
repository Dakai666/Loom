"""
Verify that ``LoomSession._dispatch_parallel`` actually overlaps tool
execution in wall-clock time. The harness has the infrastructure (#247
exposed it via the parallel envelope group panel), but until now there
was no test pinning the contract: when N independent SAFE tools are
dispatched in one batch, total wall-clock should be ~max(durations),
not the sum.

If this assertion ever flips to "sum-of-durations" we'll know
``_dispatch_parallel`` was inadvertently serialised — e.g. a middleware
acquired a per-session lock, or the dispatch path was rerouted through
a queue.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

import loom as loom_pkg
from loom.core.cognition.providers import ToolUse


@pytest.fixture(autouse=True)
def _isolate_default_registry():
    registry = loom_pkg._get_default_registry()
    original_tools = dict(registry._tools)
    registry._tools.clear()
    try:
        yield
    finally:
        registry._tools.clear()
        registry._tools.update(original_tools)


@pytest_asyncio.fixture
async def session(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A barely-started LoomSession with a single ``sleep_ms`` SAFE tool
    registered. Heavy startup paths (config / env / MCP / embedding) are
    monkeypatched out — we only need the dispatch wiring."""
    from loom.core import session as core_session
    from loom.core.session import LoomSession
    from loom.core.harness.middleware import ToolResult
    from loom.core.harness.registry import ToolDefinition
    from loom.core.harness.permissions import TrustLevel

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(core_session, "build_router", lambda: MagicMock())
    monkeypatch.setattr(core_session, "_load_loom_config", lambda: {})
    monkeypatch.setattr(core_session, "_load_env", lambda project_root=None: {})
    monkeypatch.setattr(
        core_session, "build_embedding_provider", lambda env, cfg: None
    )
    from rich.prompt import Confirm
    monkeypatch.setattr(Confirm, "ask", lambda *args, **kwargs: True)

    s = LoomSession(
        model="gpt-test",
        db_path=str(tmp_path / "loom.db"),
        workspace=workspace,
    )
    await s.start()

    async def _sleep_ms(call):
        ms = int(call.args.get("ms", 0))
        await asyncio.sleep(ms / 1000.0)
        return ToolResult(
            call_id=call.id,
            tool_name=call.tool_name,
            success=True,
            output=f"slept {ms}ms",
        )

    s.registry.register(
        ToolDefinition(
            name="sleep_ms",
            description="Test-only: sleep for the requested milliseconds",
            input_schema={
                "type": "object",
                "properties": {"ms": {"type": "integer"}},
                "required": ["ms"],
            },
            executor=_sleep_ms,
            trust_level=TrustLevel.SAFE,
        )
    )

    yield s

    await s.stop()


def _tool_uses(durations_ms: list[int]) -> list[ToolUse]:
    return [
        ToolUse(id=f"tu{i}", name="sleep_ms", args={"ms": ms})
        for i, ms in enumerate(durations_ms)
    ]


class TestParallelDispatchConcurrency:
    """Wall-clock proofs that ``_dispatch_parallel`` is actually parallel."""

    async def test_three_safe_tools_overlap_in_wall_clock(self, session):
        # Three 200ms sleeps. Sequential = 600ms, parallel ≈ 210ms.
        # Use a generous 350ms ceiling to absorb dispatch overhead on
        # slow CI runners without losing the signal
        tool_uses = _tool_uses([200, 200, 200])

        t0 = time.monotonic()
        results = await session._dispatch_parallel(tool_uses)
        elapsed_ms = (time.monotonic() - t0) * 1000

        assert len(results) == 3
        for tu, result, duration_ms in results:
            assert result.success, f"{tu.name} failed: {result.error}"
            # Each tool's own duration should be ~200ms — confirms each
            # one really slept. Combined with elapsed_ms < 350, this
            # proves overlap rather than absurdly fast no-op
            assert 150 < duration_ms < 350, (
                f"{tu.name} ran in {duration_ms:.0f}ms — sleep didn't take"
            )

        assert elapsed_ms < 350, (
            f"3× 200ms tools took {elapsed_ms:.0f}ms — looks serial "
            "(expected ~210ms parallel, hard ceiling 350ms)"
        )

    async def test_results_returned_in_input_order(self, session):
        # Decreasing durations — if results came back ordered by
        # completion (fastest first), this would fail.
        tool_uses = _tool_uses([300, 100, 200])

        results = await session._dispatch_parallel(tool_uses)

        names_in_order = [tu.id for tu, _, _ in results]
        assert names_in_order == ["tu0", "tu1", "tu2"], (
            "Result order must mirror input order regardless of completion "
            "timing — downstream code assumes positional alignment"
        )

    async def test_one_tool_failure_does_not_cancel_siblings(self, session):
        # Register a tool that always raises so we can assert siblings
        # complete despite one bad apple
        from loom.core.harness.middleware import ToolResult
        from loom.core.harness.registry import ToolDefinition
        from loom.core.harness.permissions import TrustLevel

        async def _boom(call):
            raise RuntimeError("synthetic failure")

        session.registry.register(
            ToolDefinition(
                name="boom",
                description="Test-only: always raises",
                input_schema={"type": "object"},
                executor=_boom,
                trust_level=TrustLevel.SAFE,
            )
        )

        tool_uses = [
            ToolUse(id="ok1", name="sleep_ms", args={"ms": 100}),
            ToolUse(id="bad", name="boom", args={}),
            ToolUse(id="ok2", name="sleep_ms", args={"ms": 100}),
        ]

        results = await session._dispatch_parallel(tool_uses)

        by_id = {tu.id: result for tu, result, _ in results}
        assert by_id["ok1"].success
        assert by_id["ok2"].success
        assert not by_id["bad"].success
        # The harness wraps the exception in a structured ToolResult —
        # the message just has to surface the underlying error somehow
        assert "synthetic failure" in (by_id["bad"].error or "")


class TestAllAuthorizedGate:
    """``_dispatch_parallel`` only fires when ``_all_authorized`` is True.
    This test pins the gate's behaviour so a regression turns parallel
    SAFE batches sequential without anyone noticing."""

    async def test_safe_only_batch_is_authorized(self, session):
        tool_uses = _tool_uses([10, 10])
        assert session._all_authorized(tool_uses) is True

    async def test_unknown_tool_does_not_block_authorization(self, session):
        # Unknown tools are filtered through and "fail at dispatch"
        # rather than blocking the parallel decision (per inline
        # comment in _all_authorized)
        tool_uses = [
            ToolUse(id="a", name="sleep_ms", args={"ms": 10}),
            ToolUse(id="b", name="ghost_tool", args={}),
        ]
        assert session._all_authorized(tool_uses) is True

    async def test_unauthorized_guarded_tool_blocks_parallel(self, session):
        # Register a GUARDED tool that has not been pre-authorized this
        # session — should poison _all_authorized and force sequential
        from loom.core.harness.middleware import ToolResult
        from loom.core.harness.registry import ToolDefinition
        from loom.core.harness.permissions import TrustLevel

        async def _noop(call):
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=True
            )

        session.registry.register(
            ToolDefinition(
                name="needs_auth",
                description="GUARDED test tool, never pre-authorized",
                input_schema={"type": "object"},
                executor=_noop,
                trust_level=TrustLevel.GUARDED,
            )
        )

        tool_uses = [
            ToolUse(id="a", name="sleep_ms", args={"ms": 10}),
            ToolUse(id="b", name="needs_auth", args={}),
        ]
        assert session._all_authorized(tool_uses) is False
