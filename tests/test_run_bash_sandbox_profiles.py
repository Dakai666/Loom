from __future__ import annotations

from pathlib import Path

import pytest

import loom.platform.cli.tools as tools
from loom.core.harness.middleware import BlastRadiusMiddleware, ToolCall
from loom.core.harness.permissions import PermissionContext
from loom.core.harness.registry import ToolRegistry
from loom.core.harness.scope import ConfirmDecision, ScopeGrant
from loom.core.security.sandbox_runtime import SandboxSettings


def _sandbox() -> SandboxSettings:
    return SandboxSettings.from_config({
        "backend": "srt",
        "allow_write": ["."],
        "allowed_domains": [],
        "profiles": {
            "github": {
                "match_commands": ["gh"],
                "allow_read": ["~/.config/gh"],
                "allow_write": [".", "~/.cache/gh"],
                "allowed_domains": ["api.github.com", "github.com"],
            },
        },
    })


class _FakeProc:
    returncode = 0

    async def communicate(self):
        return b"ok\n", None


def _call(tool, command: str, **extra) -> ToolCall:
    return ToolCall(
        tool_name="run_bash",
        args={"command": command, "justification": "test", **extra},
        trust_level=tool.trust_level,
        capabilities=tool.capabilities,
        session_id="test",
    )


def _authorize_profile(tool, call: ToolCall) -> None:
    call.metadata["scope_request"] = tool.scope_resolver(call)
    call.metadata["once_authorized"] = True


async def _run_through_scope_middleware(tool, call: ToolCall, perm, confirm_result):
    async def _confirm(_call):
        return confirm_result

    registry = ToolRegistry()
    registry.register(tool)
    mw = BlastRadiusMiddleware(
        perm_ctx=perm,
        confirm_fn=_confirm,
        registry=registry,
    )
    return await mw.process(call, tool.executor)


@pytest.fixture
def srt_fakes(monkeypatch):
    written = []
    wrapped = []
    launched = []

    def fake_write(settings, workspace):
        written.append(settings.to_srt_json(workspace=workspace))
        return Path(f"/tmp/srt-settings-{len(written)}.json")

    def fake_wrap(command, settings_path):
        wrapped.append((command, settings_path))
        return f"wrapped:{settings_path.name}:{command}"

    async def fake_create_subprocess_shell(command, **kwargs):
        launched.append((command, kwargs))
        return _FakeProc()

    async def fake_terminate(_proc):
        return None

    monkeypatch.setattr(tools, "srt_available", lambda: True)
    monkeypatch.setattr(tools, "_srt_write_settings_file", fake_write)
    monkeypatch.setattr(tools, "_srt_wrap_command", fake_wrap)
    monkeypatch.setattr(tools.asyncio, "create_subprocess_shell", fake_create_subprocess_shell)
    monkeypatch.setattr(tools, "_terminate_proc_group", fake_terminate)
    return written, wrapped, launched


@pytest.mark.asyncio
async def test_run_bash_denies_detected_profile_without_scope_authorization(tmp_path, srt_fakes):
    written, wrapped, launched = srt_fakes
    tool = tools.make_run_bash_tool(tmp_path, sandbox=_sandbox())
    call = _call(tool, "gh pr view 387")

    result = await tool.executor(call)

    assert not result.success
    assert result.failure_type == "permission_denied"
    assert "sandbox profile 'github'" in result.error
    assert len(written) == 1
    assert wrapped == []
    assert launched == []


@pytest.mark.asyncio
async def test_run_bash_wraps_detected_profile_after_once_authorization(tmp_path, srt_fakes):
    written, wrapped, launched = srt_fakes
    tool = tools.make_run_bash_tool(tmp_path, sandbox=_sandbox())
    call = _call(tool, "gh pr view 387")
    _authorize_profile(tool, call)

    result = await tool.executor(call)

    assert result.success
    assert len(written) == 2
    assert written[0]["network"]["allowedDomains"] == []
    assert written[1]["network"]["allowedDomains"] == ["api.github.com", "github.com"]
    assert str(Path.home() / ".config/gh") in written[1]["filesystem"]["allowRead"]
    assert wrapped == [("gh pr view 387", Path("/tmp/srt-settings-2.json"))]
    assert launched[0][0] == "wrapped:srt-settings-2.json:gh pr view 387"
    assert result.metadata["sandbox_profile"] == "github"


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [ConfirmDecision.SCOPE, ConfirmDecision.AUTO])
async def test_run_bash_applies_profile_on_scope_or_auto_first_call(
    tmp_path, srt_fakes, decision,
):
    written, _wrapped, launched = srt_fakes
    tool = tools.make_run_bash_tool(tmp_path, sandbox=_sandbox())
    call = _call(tool, "gh pr view 387")
    perm = PermissionContext(session_id="test")

    result = await _run_through_scope_middleware(tool, call, perm, decision)

    assert result.success
    assert call.metadata["user_decision"] is True
    assert result.metadata["sandbox_profile"] == "github"
    assert len(written) == 2
    assert written[1]["network"]["allowedDomains"] == ["api.github.com", "github.com"]
    assert launched[0][0] == "wrapped:srt-settings-2.json:gh pr view 387"


@pytest.mark.asyncio
async def test_run_bash_applies_profile_when_existing_grant_covers_scope(
    tmp_path, srt_fakes,
):
    written, _wrapped, launched = srt_fakes
    tool = tools.make_run_bash_tool(tmp_path, sandbox=_sandbox())
    call = _call(tool, "gh pr view 387")
    perm = PermissionContext(session_id="test")
    perm.grants.extend([
        ScopeGrant(
            resource="exec",
            action="execute",
            selector="workspace",
            constraints={"absolute_paths": "deny"},
            source="test",
        ),
        ScopeGrant(
            resource="sandbox_profile",
            action="use",
            selector="github",
            source="test",
        ),
    ])

    result = await _run_through_scope_middleware(
        tool, call, perm, ConfirmDecision.DENY,
    )

    assert result.success
    assert call.metadata["scope_verdict"].value == "allow"
    assert result.metadata["sandbox_profile"] == "github"
    assert len(written) == 2
    assert written[1]["network"]["allowedDomains"] == ["api.github.com", "github.com"]
    assert launched[0][0] == "wrapped:srt-settings-2.json:gh pr view 387"


@pytest.mark.asyncio
async def test_run_bash_async_job_uses_authorized_profile_settings(tmp_path, srt_fakes, monkeypatch):
    written, _wrapped, _launched = srt_fakes
    recorded = {}

    class JobStore:
        def submit(self, name, payload, fn):
            recorded["name"] = name
            recorded["payload"] = payload
            recorded["fn"] = fn
            return "job-1"

    async def fake_run_bash_job(command, cwd, timeout, scratchpad):
        recorded["job_command"] = command
        recorded["job_cwd"] = cwd
        recorded["job_timeout"] = timeout
        recorded["scratchpad"] = scratchpad
        return None, "exit 0, 0 chars", None

    monkeypatch.setattr(tools, "_run_bash_job", fake_run_bash_job)
    scratchpad = object()
    tool = tools.make_run_bash_tool(
        tmp_path,
        sandbox=_sandbox(),
        jobstore=JobStore(),
        scratchpad=scratchpad,
    )
    call = _call(tool, "gh api user", async_mode=True)
    _authorize_profile(tool, call)

    result = await tool.executor(call)
    await recorded["fn"]()

    assert result.success
    assert result.metadata["sandbox_profile"] == "github"
    assert recorded["payload"]["sandbox_profile"] == "github"
    assert recorded["job_command"] == "wrapped:srt-settings-2.json:gh api user"
    assert recorded["scratchpad"] is scratchpad
    assert written[1]["network"]["allowedDomains"] == ["api.github.com", "github.com"]


def test_run_bash_input_schema_accepts_explicit_sandbox_profile(tmp_path, srt_fakes):
    tool = tools.make_run_bash_tool(tmp_path, sandbox=_sandbox())
    props = tool.input_schema["properties"]

    assert "sandbox_profile" in props
