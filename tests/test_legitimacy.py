import asyncio
import pytest

from loom.core.harness.middleware import (
    ToolCall, ToolResult, MiddlewarePipeline, BlastRadiusMiddleware, LegitimacyGuardMiddleware, ToolHandler,
)
from loom.core.harness.permissions import ToolCapability
from loom.core.harness.permissions import PermissionContext, TrustLevel
from loom.core.harness.scope import ConfirmDecision
from loom.core.harness.registry import ToolRegistry, ToolDefinition
from loom.core.harness.lifecycle import ActionRecord, ActionState, LifecycleContext, LIFECYCLE_CTX_KEY, ActionIntent

@pytest.fixture
def registry():
    r = ToolRegistry()
    async def mock_executor(call): return ToolResult(call_id="", tool_name="", success=True)
    r.register(ToolDefinition(name="read_file", description="", input_schema={}, executor=mock_executor, trust_level=TrustLevel.SAFE))
    r.register(ToolDefinition(name="write_file", description="", input_schema={}, executor=mock_executor, trust_level=TrustLevel.GUARDED))
    return r

@pytest.fixture
def perm_ctx():
    return PermissionContext(session_id="test")

@pytest.fixture
def handler():
    async def mock_handler(call: ToolCall) -> ToolResult:
        return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True)
    return mock_handler

def make_call(
    name: str,
    trust: TrustLevel,
    args: dict | None = None,
    *,
    capabilities: ToolCapability = ToolCapability.NONE,
) -> ToolCall:
    call = ToolCall(
        tool_name=name, args=args or {}, trust_level=trust,
        session_id="test", capabilities=capabilities,
    )
    record = ActionRecord(call=call, intent=ActionIntent(intent_summary=name))
    ctx = LifecycleContext(record=record)
    call.metadata[LIFECYCLE_CTX_KEY] = ctx
    return call


@pytest.mark.asyncio
async def test_write_file_blocked_without_probe(handler, tmp_path):
    """Overwriting an existing file that was not read is blocked (#704 S6)."""
    (tmp_path / "foo.py").write_text("old")
    guard = LegitimacyGuardMiddleware(workspace=tmp_path)
    call_write = make_call("write_file", TrustLevel.GUARDED, {"path": "foo.py", "content": ""})
    res = await guard.process(call_write, handler)
    assert not res.success
    assert res.failure_type == "permission_denied"
    assert "守門・先讀後寫" in res.error
    assert call_write.metadata[LIFECYCLE_CTX_KEY].authorization_result is False


@pytest.mark.asyncio
async def test_write_file_allowed_after_read(handler):
    """write_file passes after any probe tool runs (has_probed=True)."""
    guard = LegitimacyGuardMiddleware()
    call_read = make_call("read_file", TrustLevel.SAFE, {"path": "foo.py"})
    await guard.process(call_read, handler)
    assert guard.has_probed

    call_write = make_call("write_file", TrustLevel.GUARDED, {"path": "foo.py", "content": ""})
    res = await guard.process(call_write, handler)
    assert res.success


@pytest.mark.asyncio
async def test_safe_tool_without_path_does_not_probe_files(handler, tmp_path):
    """Issue #288: SAFE tools that don't take a ``path`` arg (agent_health,
    introspect_self) MUST NOT count as a file probe. The previous
    sledgehammer behavior — any SAFE call clearing the gate for any
    write — is the bug this issue exists to fix.
    """
    (tmp_path / "x").write_text("x")
    guard = LegitimacyGuardMiddleware(workspace=tmp_path)
    call_safe = make_call("introspect_self", TrustLevel.SAFE, {})
    await guard.process(call_safe, handler)
    assert not guard.has_probed

    call_write = make_call("write_file", TrustLevel.GUARDED, {"path": "x", "content": ""})
    res = await guard.process(call_write, handler)
    assert not res.success
    assert res.failure_type == "permission_denied"


@pytest.mark.asyncio
async def test_explicit_read_probe_capability_counts_as_probe(handler):
    """Issue #167: GUARDED tools (e.g. web_search, MCP read tools) opt in via
    ToolCapability.READ_PROBE. Trust stays GUARDED but the probe heuristic
    recognizes the call."""
    guard = LegitimacyGuardMiddleware()
    call_search = make_call(
        "github:search_repos", TrustLevel.GUARDED, {"q": "loom"},
        capabilities=ToolCapability.NETWORK | ToolCapability.READ_PROBE,
    )
    await guard.process(call_search, handler)
    assert guard.has_probed


@pytest.mark.asyncio
async def test_guarded_tool_without_read_probe_does_not_count(handler, tmp_path):
    """Issue #167: a GUARDED tool with no READ_PROBE flag must NOT satisfy
    the heuristic, otherwise write_file could waltz through after any
    network/exec call."""
    (tmp_path / "x").write_text("x")
    guard = LegitimacyGuardMiddleware(workspace=tmp_path)
    call_unrelated = make_call(
        "some_mcp:do_thing", TrustLevel.GUARDED, {},
        capabilities=ToolCapability.NETWORK,
    )
    await guard.process(call_unrelated, handler)
    assert not guard.has_probed

    call_write = make_call("write_file", TrustLevel.GUARDED, {"path": "x", "content": ""})
    res = await guard.process(call_write, handler)
    assert not res.success
    assert res.failure_type == "permission_denied"


@pytest.mark.asyncio
async def test_run_bash_not_guarded_by_probe(handler):
    """run_bash is NOT in strict_guard_tools — BlastRadius owns exec authorization."""
    guard = LegitimacyGuardMiddleware()
    assert "run_bash" not in guard.strict_guard_tools
    call_bash = make_call("run_bash", TrustLevel.GUARDED)
    res = await guard.process(call_bash, handler)
    assert res.success, "run_bash should bypass LegitimacyGuard entirely"


# Session trust (#118: one successful write waived the check for every
# file for the rest of the session) is retired in #704 S6; see
# tests/test_guards_704_s6.py.


# Layer 2 (trajectory anomaly) only downgraded /auto, retired in #704 S3.
@pytest.mark.asyncio
async def test_exec_without_probe_is_not_flagged(handler):
    guard = LegitimacyGuardMiddleware()
    call_bash = make_call(
        "run_bash", TrustLevel.GUARDED, {"command": "ls"},
        capabilities=ToolCapability.EXEC,
    )
    res = await guard.process(call_bash, handler)
    assert res.success and "trajectory_anomaly" not in call_bash.metadata


@pytest.mark.asyncio
async def test_denials_are_counted_not_punished(perm_ctx, registry, handler):
    """#704 S4: the harness counts denials; three in a row end the turn in
    the session loop (a run stop condition), not a "penalty box" here."""
    async def always_deny(call) -> ConfirmDecision:
        return ConfirmDecision.DENY

    bmw = BlastRadiusMiddleware(perm_ctx=perm_ctx, confirm_fn=always_deny, registry=registry)
    call = make_call("write_file", TrustLevel.GUARDED)
    call.abort_signal = asyncio.Event()
    for n in (1, 2, 3):
        res = await bmw.process(call, handler)
        assert not res.success and "Penalty" not in res.error
        assert perm_ctx.recent_denies == n
    assert not call.metadata.get("circuit_breaker") and not call.abort_signal.is_set()

@pytest.mark.asyncio
async def test_circuit_breaker_resets_on_allow(perm_ctx, registry, handler):
    decision = ConfirmDecision.DENY
    async def mock_confirm(call) -> ConfirmDecision:
        return decision

    bmw = BlastRadiusMiddleware(
        perm_ctx=perm_ctx,
        confirm_fn=mock_confirm,
        registry=registry,
    )
    
    call = make_call("write_file", TrustLevel.GUARDED)
    
    # Deny 1 & 2
    await bmw.process(call, handler)
    await bmw.process(call, handler)
    assert perm_ctx.recent_denies == 2
    
    # Allow resets
    decision = ConfirmDecision.ONCE
    res3 = await bmw.process(call, handler)
    assert res3.success
    assert perm_ctx.recent_denies == 0


@pytest.mark.asyncio
async def test_readonly_bash_counts_as_probe(handler):
    """Issue #283: grep, head, cat, etc. set has_probed."""
    guard = LegitimacyGuardMiddleware()

    call_grep = make_call(
        "run_bash", TrustLevel.GUARDED,
        {"command": "grep -n 'class ' file.py"},
        capabilities=ToolCapability.EXEC,
    )
    await guard.process(call_grep, handler)
    assert guard.has_probed


@pytest.mark.asyncio
async def test_sed_minus_n_counts_as_probe(handler):
    """sed -n is read-only and should count as a probe."""
    guard = LegitimacyGuardMiddleware()

    call_sed = make_call(
        "run_bash", TrustLevel.GUARDED,
        {"command": "sed -n '100,120p' file.py"},
        capabilities=ToolCapability.EXEC,
    )
    await guard.process(call_sed, handler)
    assert guard.has_probed


@pytest.mark.asyncio
async def test_sed_without_n_does_not_count(handler):
    """sed without -n is mutating (sed -i ...), NOT a probe."""
    guard = LegitimacyGuardMiddleware()

    call_sed = make_call(
        "run_bash", TrustLevel.GUARDED,
        {"command": "sed -i 's/old/new/' file.py"},
        capabilities=ToolCapability.EXEC,
    )
    await guard.process(call_sed, handler)
    assert not guard.has_probed


@pytest.mark.asyncio
async def test_unknown_command_does_not_count(handler):
    """Arbitrary commands like python3 are not whitelisted."""
    guard = LegitimacyGuardMiddleware()

    call_py = make_call(
        "run_bash", TrustLevel.GUARDED,
        {"command": "python3 -c 'print(1)'"},
        capabilities=ToolCapability.EXEC,
    )
    await guard.process(call_py, handler)
    assert not guard.has_probed


@pytest.mark.asyncio
async def test_readonly_bash_permits_write(handler):
    """After a read-only bash, write_file should be allowed."""
    guard = LegitimacyGuardMiddleware()

    call_grep = make_call(
        "run_bash", TrustLevel.GUARDED,
        {"command": "grep class file.py"},
        capabilities=ToolCapability.EXEC,
    )
    await guard.process(call_grep, handler)

    call_write = make_call(
        "write_file", TrustLevel.GUARDED,
        {"path": "file.py", "content": "x"},
    )
    res = await guard.process(call_write, handler)
    assert res.success


@pytest.mark.asyncio
async def test_per_file_probe_blocks_unrelated_write(handler, tmp_path):
    """Issue #288: probing foo.py does NOT clear writes to bar.py."""
    (tmp_path / "foo.py").write_text("f")
    (tmp_path / "bar.py").write_text("b")
    guard = LegitimacyGuardMiddleware(workspace=tmp_path)

    await guard.process(
        make_call("read_file", TrustLevel.SAFE, {"path": "foo.py"}), handler
    )

    res_foo = await guard.process(
        make_call("write_file", TrustLevel.GUARDED, {"path": "foo.py", "content": "x"}),
        handler,
    )
    assert res_foo.success

    res_bar = await guard.process(
        make_call("write_file", TrustLevel.GUARDED, {"path": "bar.py", "content": "x"}),
        handler,
    )
    assert not res_bar.success
    assert res_bar.failure_type == "permission_denied"


@pytest.mark.asyncio
async def test_probe_file_tool_marks_specific_path(handler, tmp_path):
    """probe_file('x.py') marks x.py specifically, not all files."""
    (tmp_path / "x.py").write_text("x")
    (tmp_path / "y.py").write_text("y")
    guard = LegitimacyGuardMiddleware(workspace=tmp_path)

    await guard.process(
        make_call("probe_file", TrustLevel.SAFE, {"path": "x.py"}), handler
    )
    res_y = await guard.process(
        make_call("write_file", TrustLevel.GUARDED, {"path": "y.py", "content": "x"}), handler,
    )
    assert not res_y.success

    res = await guard.process(
        make_call("write_file", TrustLevel.GUARDED, {"path": "x.py", "content": "x"}),
        handler,
    )
    assert res.success


@pytest.mark.asyncio
async def test_bash_path_extraction_per_file(handler, tmp_path):
    """grep PAT file.py marks file.py, not other.py."""
    (tmp_path / "file.py").write_text("f")
    (tmp_path / "other.py").write_text("o")
    guard = LegitimacyGuardMiddleware(workspace=tmp_path)

    await guard.process(
        make_call(
            "run_bash", TrustLevel.GUARDED,
            {"command": "grep class file.py"},
            capabilities=ToolCapability.EXEC,
        ),
        handler,
    )
    res_match = await guard.process(
        make_call("write_file", TrustLevel.GUARDED, {"path": "file.py", "content": "x"}),
        handler,
    )
    assert res_match.success

    res_other = await guard.process(
        make_call("write_file", TrustLevel.GUARDED, {"path": "other.py", "content": "x"}),
        handler,
    )
    assert not res_other.success


@pytest.mark.asyncio
async def test_bash_pipeline_unlocks_no_file(handler, tmp_path):
    """Pipelines defeat the parser: the read names no file, so it unlocks
    no overwrite (#704 S6; it used to unlock every file)."""
    (tmp_path / "anything.py").write_text("a")
    guard = LegitimacyGuardMiddleware(workspace=tmp_path)

    await guard.process(
        make_call(
            "run_bash", TrustLevel.GUARDED,
            {"command": "cat a.py | grep class"},
            capabilities=ToolCapability.EXEC,
        ),
        handler,
    )
    assert guard.has_probed

    res = await guard.process(
        make_call("write_file", TrustLevel.GUARDED, {"path": "anything.py", "content": "x"}),
        handler,
    )
    assert not res.success


@pytest.mark.asyncio
async def test_path_normalization_dot_prefix(handler, tmp_path):
    """./foo.py and foo.py refer to the same file under per-file tracking."""
    (tmp_path / "foo.py").write_text("f")
    guard = LegitimacyGuardMiddleware(workspace=tmp_path)

    await guard.process(
        make_call("read_file", TrustLevel.SAFE, {"path": "./foo.py"}), handler
    )

    res = await guard.process(
        make_call("write_file", TrustLevel.GUARDED, {"path": "foo.py", "content": "x"}),
        handler,
    )
    assert res.success


@pytest.mark.asyncio
async def test_read_probe_capability_unscoped(handler):
    """READ_PROBE-flagged tools (web_search, MCP) probe unscoped — they
    don't read local files but the agent has done some real reading work."""
    guard = LegitimacyGuardMiddleware()

    await guard.process(
        make_call(
            "github:search", TrustLevel.GUARDED, {"q": "loom"},
            capabilities=ToolCapability.NETWORK | ToolCapability.READ_PROBE,
        ),
        handler,
    )
    assert guard.has_probed


@pytest.mark.asyncio
async def test_probed_files_persist_across_turn_reset(handler, tmp_path):
    """reset_probe() clears the per-turn flag; a read survives the turn
    while the file stays unchanged."""
    (tmp_path / "kept.py").write_text("k")
    guard = LegitimacyGuardMiddleware(workspace=tmp_path)

    await guard.process(
        make_call("read_file", TrustLevel.SAFE, {"path": "kept.py"}), handler
    )
    guard.reset_probe()
    assert not guard.has_probed

    res = await guard.process(
        make_call("write_file", TrustLevel.GUARDED, {"path": "kept.py", "content": "x"}),
        handler,
    )
    assert res.success


@pytest.mark.asyncio
async def test_probe_file_tool_executes_successfully():
    """probe_file executor must build a valid ToolResult — would catch the
    `text=` vs `output=` typo that initially shipped in #287.
    """
    from loom.platform.cli.tools import make_probe_file_tool

    td = make_probe_file_tool()
    call = make_call("probe_file", TrustLevel.SAFE, {"path": "/tmp/foo.py"})
    res = await td.executor(call)
    assert res.success
    assert "/tmp/foo.py" in res.output
