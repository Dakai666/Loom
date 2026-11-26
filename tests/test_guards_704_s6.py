"""
Contract: #704 S6 — the guards around the permission mode.

1. Read before write (write_file): creating a file that does not exist at
   call time needs no prior read. Overwriting an existing file needs its
   current content read in this session, unchanged since. One successful
   write never waives the check for other files, and a probe that names no
   file (web search, a shell pipeline) never unlocks every file. The tool
   re-checks the same expectation where it writes, so a file that appears
   or changes while DK is being asked is not overwritten unseen.
2. One readable reason: whichever guard layer stops or asks (permission
   mode, hard guardrail, read before write, scope, schema), the error or the
   confirm carries one line in one format, the same on CLI and Discord.
3. The permission mode is shown in the CLI footer and on every confirm.
4. A sub-agent's mode is 安全 whatever the parent's.
5. The retired per-batch pause path is gone.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from loom.core.harness.lifecycle import LIFECYCLE_CTX_KEY, ActionIntent, ActionRecord, LifecycleContext
from loom.core.harness.middleware import LegitimacyGuardMiddleware, ToolCall, ToolResult
from loom.core.harness.mode import PermissionMode
from loom.core.harness.permissions import PermissionContext, ToolCapability, TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry


async def _ok(call: ToolCall) -> ToolResult:
    return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True)


def _call(name, args, *, trust=TrustLevel.GUARDED, caps=ToolCapability.NONE) -> ToolCall:
    call = ToolCall(tool_name=name, args=args, trust_level=trust, session_id="t", capabilities=caps)
    call.metadata[LIFECYCLE_CTX_KEY] = LifecycleContext(
        record=ActionRecord(call=call, intent=ActionIntent(intent_summary=name)))
    return call


def _guard(ws: Path) -> LegitimacyGuardMiddleware:
    return LegitimacyGuardMiddleware(workspace=ws)


async def _read(guard, path):
    return await guard.process(_call("read_file", {"path": path}, trust=TrustLevel.SAFE), _ok)


async def _write(guard, path, content="x", handler=_ok):
    return await guard.process(_call("write_file", {"path": path, "content": content}), handler)


# ---------------------------------------------------------------------------
# 1. Read before write
# ---------------------------------------------------------------------------


class TestReadBeforeWrite:
    async def test_new_file_needs_no_read(self, tmp_path):
        r = await _write(_guard(tmp_path), "notes/new.md")
        assert r.success, r.error

    async def test_existing_file_needs_a_read(self, tmp_path):
        (tmp_path / "a.md").write_text("old")
        r = await _write(_guard(tmp_path), "a.md")
        assert not r.success and r.failure_type == "permission_denied"
        assert "守門・先讀後寫：" in r.error

    async def test_existing_file_after_a_read(self, tmp_path):
        (tmp_path / "a.md").write_text("old")
        g = _guard(tmp_path)
        await _read(g, "a.md")
        assert (await _write(g, "a.md")).success

    async def test_a_read_of_a_changed_file_is_stale(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("old")
        g = _guard(tmp_path)
        await _read(g, "a.md")
        f.write_text("changed by someone else")
        r = await _write(g, "a.md")
        assert not r.success and "守門・先讀後寫：" in r.error and "changed" in r.error

    async def test_one_write_does_not_waive_the_check_for_other_files(self, tmp_path):
        (tmp_path / "a.md").write_text("a")
        (tmp_path / "b.md").write_text("b")
        g = _guard(tmp_path)
        await _read(g, "a.md")
        assert (await _write(g, "a.md")).success
        g.reset_probe()
        assert not (await _write(g, "b.md")).success

    async def test_own_write_counts_as_known_content(self, tmp_path):
        g = _guard(tmp_path)

        async def really_write(call):
            (tmp_path / call.args["path"]).write_text(call.args["content"])
            return await _ok(call)

        assert (await _write(g, "n.md", "v1", really_write)).success
        assert (await _write(g, "n.md", "v2", really_write)).success

    @pytest.mark.parametrize("probe", [
        _call("web_search", {"query": "x"}, caps=ToolCapability.NETWORK | ToolCapability.READ_PROBE),
        _call("run_bash", {"command": "cat a.md | head"}, caps=ToolCapability.EXEC),
    ])
    async def test_a_probe_without_a_file_unlocks_nothing(self, tmp_path, probe):
        (tmp_path / "a.md").write_text("a")
        g = _guard(tmp_path)
        await g.process(probe, _ok)
        assert not (await _write(g, "a.md")).success

    @pytest.mark.parametrize("read_as", ["./a.md", "sub/../a.md", "{ws}/a.md"])
    async def test_one_file_under_any_spelling(self, tmp_path, read_as):
        (tmp_path / "a.md").write_text("a")
        g = _guard(tmp_path)
        await _read(g, read_as.format(ws=tmp_path))
        assert (await _write(g, "a.md")).success

    async def test_readonly_bash_on_the_file_still_counts(self, tmp_path):
        (tmp_path / "a.md").write_text("a")
        g = _guard(tmp_path)
        await g.process(_call("run_bash", {"command": "cat a.md"}, caps=ToolCapability.EXEC), _ok)
        assert (await _write(g, "a.md")).success


class TestWriteFileChecksAtTheActionPoint:
    """The guard's expectation rides on the call; write_file re-checks it
    where it writes (a confirm can take minutes)."""

    def _tool(self, ws):
        from loom.platform.cli.tools import make_filesystem_tools
        return next(t for t in make_filesystem_tools(ws) if t.name == "write_file")

    async def test_file_appeared_since_the_check(self, tmp_path):
        g = _guard(tmp_path)
        tool = self._tool(tmp_path)

        async def appears_then_write(call):
            (tmp_path / "a.md").write_text("someone else's")
            return await tool.executor(call)

        r = await _write(g, "a.md", "mine", appears_then_write)
        assert not r.success and "守門・先讀後寫：" in r.error
        assert (tmp_path / "a.md").read_text() == "someone else's"

    async def test_file_changed_since_the_check(self, tmp_path):
        (tmp_path / "a.md").write_text("old")
        g = _guard(tmp_path)
        await _read(g, "a.md")
        tool = self._tool(tmp_path)

        async def changes_then_write(call):
            (tmp_path / "a.md").write_text("changed")
            return await tool.executor(call)

        r = await _write(g, "a.md", "mine", changes_then_write)
        assert not r.success and "守門・先讀後寫：" in r.error
        assert (tmp_path / "a.md").read_text() == "changed"

    async def test_unchanged_file_is_written(self, tmp_path):
        (tmp_path / "a.md").write_text("old")
        g = _guard(tmp_path)
        await _read(g, "a.md")
        r = await _write(g, "a.md", "mine", self._tool(tmp_path).executor)
        assert r.success and (tmp_path / "a.md").read_text() == "mine"


# ---------------------------------------------------------------------------
# 2 + 3. One reason, one format; the mode on every confirm
# ---------------------------------------------------------------------------


def _asking_call(note=None, *, mode=PermissionMode.SELF, verdict=None, diff=None, request=None):
    from loom.core.harness.guard_reason import ask_reason
    call = _call("write_file", {"path": "x.md", "content": "x"})
    call.metadata["permission_mode"] = mode.value
    if verdict is not None:
        call.metadata["scope_verdict"] = verdict
        call.metadata["scope_diff"] = diff
        call.metadata["scope_request"] = request
    call.metadata["guard_reason"] = ask_reason(note, verdict, diff)
    return call


class TestOneReasonLine:
    @pytest.mark.parametrize("note,layer", [
        ("hard: secret_path", "硬護欄"),
        ("model-abstain", "權限模式"),
        ("rule-refuse: unknown_program", "權限模式"),
        ("", "權限模式"),
        ("error", "權限模式"),
        ("rule-refuse: scope_ceiling", "授權範圍"),
    ])
    def test_confirm_line_names_mode_and_layer(self, note, layer):
        from loom.core.harness.guard_reason import confirm_line
        line = confirm_line(_asking_call(note))
        assert line.startswith("[自理] 守門・") and f"守門・{layer}：" in line

    def test_expand_scope_without_a_classifier_is_the_scope_layer(self):
        from loom.core.harness.guard_reason import confirm_line
        from loom.core.harness.scope import (
            DiffReason, PermissionVerdict, ScopeDiff, ScopeRequest, ScopeRequirement,
        )
        req = ScopeRequirement(resource="path", action="write", selector="notes/x.md")
        diff = ScopeDiff(covered=[], missing=[req], reason=DiffReason.SELECTOR_EXPANSION)
        call = _asking_call("", mode=PermissionMode.SAFE, verdict=PermissionVerdict.EXPAND_SCOPE,
                            diff=diff, request=ScopeRequest(tool_name="write_file",
                                                            capabilities=ToolCapability.NONE,
                                                            requirements=[req]))
        line = confirm_line(call)
        assert line.startswith("[安全] 守門・授權範圍：") and "notes/x.md" in line

    async def test_cli_and_discord_show_the_same_line(self, monkeypatch):
        pytest.importorskip("discord")
        from loom.core.harness.guard_reason import confirm_line
        from loom.core.session_support import _format_scope_panel, _format_scope_summary_plain
        from loom.platform.discord import bot as bot_mod

        call = _asking_call("model-abstain")
        line = confirm_line(call)
        assert line in _format_scope_summary_plain(call)
        assert line in _format_scope_panel(call)

        sent: list[str] = []

        async def fake_send(channel, text, **kw):
            sent.append(text)
            return None

        class _View:
            async def wait_decision(self):
                from loom.core.harness.scope import ConfirmDecision
                return ConfirmDecision.DENY

        monkeypatch.setattr(bot_mod, "_safe_send", fake_send)
        monkeypatch.setattr(bot_mod, "_ConfirmView", _View)
        b = bot_mod.LoomDiscordBot.__new__(bot_mod.LoomDiscordBot)
        b._client = SimpleNamespace(get_channel=lambda tid: object())
        b._active_confirmations = {}
        await b._make_confirm_fn(1)(call)
        assert sent and line in sent[0]

    async def test_discord_confirm_shows_the_mode_even_without_a_classifier_note(self, monkeypatch):
        pytest.importorskip("discord")
        from loom.platform.discord import bot as bot_mod
        call = _call("memorize", {"content": "x"})
        call.metadata["permission_mode"] = "safe"
        sent: list[str] = []

        async def fake_send(channel, text, **kw):
            sent.append(text)

        class _View:
            async def wait_decision(self):
                from loom.core.harness.scope import ConfirmDecision
                return ConfirmDecision.DENY

        monkeypatch.setattr(bot_mod, "_safe_send", fake_send)
        monkeypatch.setattr(bot_mod, "_ConfirmView", _View)
        b = bot_mod.LoomDiscordBot.__new__(bot_mod.LoomDiscordBot)
        b._client = SimpleNamespace(get_channel=lambda tid: object())
        b._active_confirmations = {}
        await b._make_confirm_fn(1)(call)
        assert "[安全]" in sent[0]


class TestStopsCarryTheSameLine:
    async def test_schema(self):
        from loom.core.harness.validation import SchemaValidationMiddleware
        reg = ToolRegistry()
        reg.register(ToolDefinition(
            name="t", description="", executor=_ok, trust_level=TrustLevel.SAFE,
            input_schema={"type": "object", "properties": {"n": {"type": "integer"}},
                          "required": ["n"]}))
        r = await SchemaValidationMiddleware(reg).process(_call("t", {}), _ok)
        assert not r.success and r.error.startswith("守門・參數格式：")

    async def test_read_before_write(self, tmp_path):
        (tmp_path / "a.md").write_text("a")
        r = await _write(_guard(tmp_path), "a.md")
        assert r.error.startswith("守門・先讀後寫：")

    async def test_scope_deny_and_unattended_skip(self, tmp_path):
        from loom.core.harness.middleware import BlastRadiusMiddleware

        reg = ToolRegistry()
        reg.register(ToolDefinition(name="memorize", description="", input_schema={},
                                    executor=_ok, trust_level=TrustLevel.GUARDED))
        perm = PermissionContext(session_id="t", mode=PermissionMode.SAFE)

        async def confirm(call):
            raise AssertionError("unattended never asks")

        blast = BlastRadiusMiddleware(perm_ctx=perm, confirm_fn=confirm, registry=reg)
        call = _call("memorize", {"content": "x"})
        call.origin = "chime"
        r = await blast.process(call, _ok)
        assert not r.success and r.metadata.get("unattended_skip")
        assert r.error.startswith("守門・權限模式：")

    def test_hard_layer_line(self):
        from loom.core.harness.guard_reason import ask_reason
        reason = ask_reason("hard: secret_path", None, None)
        assert reason.line() == "守門・硬護欄：secret_path"


class TestFooterShowsTheMode:
    def test_footer_renders_the_mode_label(self):
        from loom.platform.cli.app import build_loom_app
        app = build_loom_app(on_submit=lambda text: None)
        app.footer.model = "m"
        app.footer.permission_mode = lambda: PermissionMode.YOLO
        text = "".join(t for _, t in app._render_footer())
        assert "YOLO" in text
        app.footer.permission_mode = lambda: PermissionMode.SELF
        assert "自理" in "".join(t for _, t in app._render_footer())


# ---------------------------------------------------------------------------
# 4. Sub-agent mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("parent", list(PermissionMode))
def test_subagent_mode_is_never_wider_than_the_parent(parent):
    from loom.core.agent.subagent import subagent_mode
    assert subagent_mode(parent) is PermissionMode.SAFE


# ---------------------------------------------------------------------------
# 5. The pause path is gone
# ---------------------------------------------------------------------------


def test_pause_path_is_gone():
    from loom.core import events
    from loom.core.session import LoomSession
    from loom.platform.cli.app import LoomApp
    assert not hasattr(events, "TurnPaused")
    for gone in ("pause", "resume", "resume_with"):
        assert not hasattr(LoomSession, gone), gone
    assert not hasattr(LoomApp, "request_pause")


# ---------------------------------------------------------------------------
# Session wiring: the guard resolves paths in the session's workspace
# ---------------------------------------------------------------------------


async def test_session_guard_resolves_in_the_workspace(monkeypatch, tmp_path):
    from loom.core import session as session_module
    from loom.core.session import LoomSession

    ws = tmp_path / "workspace"
    ws.mkdir()
    (tmp_path / "home").mkdir()
    (ws / "kept.md").write_text("k")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(session_module, "build_router", lambda *a, **k: MagicMock())
    monkeypatch.setattr(session_module, "_load_loom_config", lambda: {})
    monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {})
    monkeypatch.setattr(session_module, "build_embedding_provider", lambda env, cfg: None)
    from rich.prompt import Confirm
    monkeypatch.setattr(Confirm, "ask", lambda *a, **k: True)
    session = LoomSession(model="gpt-test", db_path=str(tmp_path / "loom.db"), workspace=ws)
    await session.start()
    try:
        g = session._legitimacy_guard
        assert not (await _write(g, "kept.md")).success
        assert (await _write(g, "fresh.md")).success
    finally:
        await session.stop()


# ---------------------------------------------------------------------------
# #711 review: a failed read, a spaced path, a FIFO
# ---------------------------------------------------------------------------


async def _fail(call: ToolCall) -> ToolResult:
    return ToolResult(call_id=call.id, tool_name=call.tool_name, success=False, error="nope")


class TestReviewFindings:
    async def test_a_failed_read_is_not_a_read(self, tmp_path):
        (tmp_path / "bin.dat").write_bytes(b"\xff\xfe\x00")
        g = _guard(tmp_path)
        await g.process(_call("read_file", {"path": "bin.dat"}, trust=TrustLevel.SAFE), _fail)
        assert not (await _write(g, "bin.dat")).success

    async def test_real_read_file_on_a_binary_unlocks_nothing(self, tmp_path):
        from loom.platform.cli.tools import make_filesystem_tools
        (tmp_path / "bin.dat").write_bytes(b"\xff\xfe\x00")
        tools = {t.name: t for t in make_filesystem_tools(tmp_path)}
        g = _guard(tmp_path)
        r = await g.process(_call("read_file", {"path": "bin.dat"}, trust=TrustLevel.SAFE),
                            tools["read_file"].executor)
        assert not r.success
        w = await _write(g, "bin.dat", "x", tools["write_file"].executor)
        assert not w.success and (tmp_path / "bin.dat").read_bytes() == b"\xff\xfe\x00"

    async def test_probe_file_stays_a_self_declared_unlock(self, tmp_path):
        from loom.platform.cli.tools import make_probe_file_tool
        (tmp_path / "a.md").write_text("a")
        g = _guard(tmp_path)
        await g.process(_call("probe_file", {"path": "a.md"}, trust=TrustLevel.SAFE),
                        make_probe_file_tool().executor)
        assert (await _write(g, "a.md")).success

    @pytest.mark.parametrize("raw", [" spaced.md", "spaced.md ", "\tspaced.md"])
    async def test_a_spaced_path_names_the_same_file_in_guard_and_tool(self, tmp_path, raw):
        from loom.platform.cli.tools import make_filesystem_tools
        target = tmp_path / raw
        target.write_text("unread")
        write = next(t for t in make_filesystem_tools(tmp_path) if t.name == "write_file")
        r = await _write(_guard(tmp_path), raw, "mine", write.executor)
        assert not r.success and "守門・先讀後寫：" in r.error
        assert target.read_text() == "unread"

    async def test_tool_fails_closed_when_the_expectation_names_another_file(self, tmp_path):
        from loom.platform.cli.tools import make_filesystem_tools
        (tmp_path / "a.md").write_text("keep")
        write = next(t for t in make_filesystem_tools(tmp_path) if t.name == "write_file")
        call = _call("write_file", {"path": "a.md", "content": "x"})
        call.metadata["write_expect"] = {"path": str(tmp_path / "other.md"), "sha256": None}
        r = await write.executor(call)
        assert not r.success and "守門・先讀後寫：" in r.error
        assert (tmp_path / "a.md").read_text() == "keep"

    def test_a_fifo_does_not_block(self, tmp_path):
        import asyncio
        import os
        import threading
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        out: list = []
        t = threading.Thread(target=lambda: out.append(asyncio.run(_write(_guard(tmp_path), "pipe"))),
                             daemon=True)
        t.start()
        t.join(timeout=2)
        if t.is_alive():  # unblock the reader so the thread can end
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
            t.join(timeout=2)
            pytest.fail("the guard blocked on a FIFO")
        assert not out[0].success and "守門・先讀後寫：" in out[0].error

    def test_digest_is_chunked_and_regular_files_only(self, tmp_path):
        import hashlib
        import os
        from loom.core.harness.paths import file_digest
        big = tmp_path / "big.bin"
        data = os.urandom(3 * 1024 * 1024 + 7)
        big.write_bytes(data)
        assert file_digest(big) == hashlib.sha256(data).hexdigest()
        assert file_digest(tmp_path / "missing") is None
        assert file_digest(tmp_path) not in (None, hashlib.sha256(b"").hexdigest())
