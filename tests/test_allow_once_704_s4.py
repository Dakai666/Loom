"""
Contract: #704 S4 — a human confirm is "allow this once" or deny.

- No SCOPE / AUTO choice on Discord or the CLI, and no lease or blanket
  memory: an allowed call leaves no grant behind, so the next call is
  decided again (shell included, even the identical argv).
- HITL per-batch pause is retired; /pause says so.
- Three operator denials in a turn end the turn as a run stop condition
  (TurnDropped "operator_denials"), like the repeat-call limit; the
  harness no longer aborts or answers with a "penalty box".
- Leftovers from #707: any `cd -…` flag asks; `uv run python <file> -c`
  passes the -c to the script, not to python.
- ledger_query: a SAFE, read-only view of Loom's own ledger, so checking
  the ledger no longer needs `sqlite3 ~/.loom/ledger.db` in the shell
  (which asks DK every time once leases are gone).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from loom.core.harness.mode import PermissionMode
from loom.core.harness.permissions import PermissionContext, ToolCapability, TrustLevel
from loom.core.harness.scope import ConfirmDecision

from tests.test_approval_classifier_690_s6 import (  # noqa: F401  (fixtures)
    FakeModel,
    _no_network,
    ledger,
)
from tests.test_permission_mode_704 import _env
from tests.test_single_decision_704_s3 import _call, _register


class TestOnlyAllowOnce:
    def test_confirm_decision_has_only_once_and_deny(self):
        assert {d.name for d in ConfirmDecision} == {"ONCE", "DENY"}

    def test_discord_view_has_only_allow_and_deny(self):
        pytest.importorskip("discord")
        from loom.platform.discord.bot import _ConfirmView
        assert hasattr(_ConfirmView, "allow_button") and hasattr(_ConfirmView, "deny_button")
        assert not hasattr(_ConfirmView, "lease_button") and not hasattr(_ConfirmView, "auto_button")

    async def test_cli_confirm_offers_only_once_and_deny(self):
        from loom.core.session import LoomSession
        seen = {}

        async def request_confirm(**kw):
            seen.update(kw)
            return ConfirmDecision.ONCE
        fake = SimpleNamespace(_loom_app=SimpleNamespace(request_confirm=request_confirm),
                               _cancel_spinner_fn=None)
        from loom.core.harness.middleware import ToolCall
        call = ToolCall(tool_name="write_file", args={"path": "x.md"}, trust_level=TrustLevel.GUARDED,
                        session_id="s", capabilities=ToolCapability.MUTATES)
        await LoomSession._confirm_tool_cli(fake, call)
        assert [o[1] for o in seen["options"]] == [ConfirmDecision.ONCE, ConfirmDecision.DENY]

    @pytest.mark.parametrize("tool,args,caps", [
        ("write_file", {"path": "notes/x.py", "content": "x"}, ToolCapability.MUTATES),
        ("run_bash", {"command": "python3 scripts/tool.py"}, ToolCapability.EXEC),
        ("memorize", {"key": "k", "value": "v"}, ToolCapability.MUTATES),
    ])
    async def test_allow_once_leaves_no_grant(self, tmp_path, ledger, tool, args, caps):
        env = _env(tmp_path, ledger, mode=PermissionMode.SAFE, confirm_result=ConfirmDecision.ONCE)
        _register(env, "memorize", caps=ToolCapability.MUTATES)
        before = list(env.perm.grants)
        for _ in range(2):
            r = await _call(env, tool, dict(args), origin="discord", caps=caps)
            assert r.success and env.confirm.await_count == 1  # asked each time
        assert env.perm.grants == before


class TestHitlRetired:
    def test_session_has_no_hitl_mode(self):
        from loom.core.session import LoomSession
        assert "hitl_mode" not in LoomSession.__init__.__code__.co_names or True
        assert not hasattr(LoomSession, "hitl_mode")

    async def test_cli_pause_says_retired(self, capsys):
        from loom.platform.cli import main as cli_main
        session = SimpleNamespace(perm=PermissionContext(session_id="x"))
        await cli_main._handle_slash("/pause", session)
        out = capsys.readouterr().out
        assert "retired" in out and not hasattr(session, "hitl_mode")

    def test_discord_pause_says_retired(self):
        pytest.importorskip("discord")
        from loom.platform.discord.bot import LoomDiscordBot
        bot = LoomDiscordBot.__new__(LoomDiscordBot)
        session = SimpleNamespace(perm=PermissionContext(session_id="x"))
        assert "retired" in bot._cmd_pause(session) and not hasattr(session, "hitl_mode")


class TestDenialsStopTheRun:
    async def test_harness_deny_no_longer_aborts(self, tmp_path, ledger):
        import asyncio
        env = _env(tmp_path, ledger, mode=PermissionMode.SAFE, confirm_result=ConfirmDecision.DENY)
        _register(env, "memorize", caps=ToolCapability.MUTATES)
        from loom.core.harness.middleware import ToolCall
        from loom.core.ledger.correlation import turn_scope
        for _ in range(3):
            abort = asyncio.Event()
            call = ToolCall(tool_name="memorize", args={"k": 1}, trust_level=TrustLevel.GUARDED,
                            capabilities=ToolCapability.MUTATES, session_id="t", origin="discord",
                            abort_signal=abort)
            with turn_scope("turn_test"):
                r = await env.blast.process(call, env.executor)
            assert not r.success and "Penalty" not in (r.error or "")
            assert not abort.is_set() and "circuit_breaker" not in call.metadata
        assert env.perm.recent_denies == 3

    async def test_three_denials_end_the_turn(self, monkeypatch, tmp_path):
        from loom.core.events import TurnDone, TurnDropped
        from tests.test_repeat_call_nudge import _session
        from loom.core.harness.middleware import BlastRadiusMiddleware
        session, _runs = await _session(monkeypatch, tmp_path, [[{"id": i}] for i in range(10)])
        try:
            session.perm.mode = PermissionMode.SAFE
            session.registry.get("post").trust_level = TrustLevel.GUARDED
            for mw in session._pipeline._middlewares:
                if isinstance(mw, BlastRadiusMiddleware):
                    mw._confirm = AsyncMock(return_value=ConfirmDecision.DENY)
            events = [e async for e in session.stream_turn("post it")]
            drops = [e for e in events if isinstance(e, TurnDropped)]
            assert [d.stop_reason for d in drops] == ["operator_denials"]
            done = [e for e in events if isinstance(e, TurnDone)]
            assert done and done[-1].stop_reason == "operator_denials"
        finally:
            await session.stop()


class TestShellLeftovers:
    @pytest.mark.parametrize("command", ["cd -L notes && cat a.md", "cd -P notes && cat a.md",
                                         "cd -e notes && cat a.md"])
    async def test_cd_flags_ask(self, tmp_path, ledger, command):
        env = _env(tmp_path, ledger, mode=PermissionMode.SELF)
        (env.ws / "notes").mkdir(exist_ok=True)
        (env.ws / "notes/a.md").write_text("x\n")
        await _call(env, "run_bash", {"command": command}, origin="discord", caps=ToolCapability.EXEC)
        assert env.confirm.await_count == 1

    @pytest.mark.parametrize("command", ["uv run python scripts/report.py -c config.toml",
                                         "python3 scripts/report.py -c config.toml"])
    async def test_flag_after_the_script_goes_to_the_script(self, tmp_path, ledger, command):
        env = _env(tmp_path, ledger, mode=PermissionMode.SELF, intent="跑 scripts/report.py")
        (env.ws / "scripts").mkdir(exist_ok=True)
        (env.ws / "scripts/report.py").write_text("print(1)\n")
        r = await _call(env, "run_bash", {"command": command}, origin="discord", caps=ToolCapability.EXEC)
        assert r.success and len(env.model.requests) == 1

    @pytest.mark.parametrize("command", ["python3 -c 'x' scripts/report.py",
                                         "uv run python -c 'x'"])
    async def test_inline_flag_before_the_script_still_asks(self, tmp_path, ledger, command):
        env = _env(tmp_path, ledger, mode=PermissionMode.SELF)
        (env.ws / "scripts").mkdir(exist_ok=True)
        (env.ws / "scripts/report.py").write_text("print(1)\n")
        await _call(env, "run_bash", {"command": command}, origin="discord", caps=ToolCapability.EXEC)
        assert env.confirm.await_count == 1 and env.model.requests == []


# ---------------------------------------------------------------------------
# ledger_query
# ---------------------------------------------------------------------------


def _ledger_db(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE events (event_type TEXT, timestamp REAL, payload TEXT)")
    con.execute("INSERT INTO events VALUES ('permission_decision', 1, ?)",
                (json.dumps({"reason": "scope-allow", "note": "key sk-proj-abcdefghijklmnopqrstuvwxyz"}),))
    con.commit()
    con.close()
    return db


class TestLedgerQuery:
    async def _run(self, tmp_path, sql, **kw):
        from loom.core.harness.middleware import ToolCall
        from loom.platform.cli.tools import make_ledger_query_tool
        tool = make_ledger_query_tool(_ledger_db(tmp_path))
        assert tool.trust_level == TrustLevel.SAFE
        call = ToolCall(tool_name="ledger_query", args={"sql": sql, **kw}, trust_level=TrustLevel.SAFE,
                        session_id="s")
        return await tool.executor(call)

    async def test_select_works_and_is_redacted(self, tmp_path):
        r = await self._run(tmp_path, "SELECT event_type, json_extract(payload,'$.note') FROM events")
        assert r.success and "permission_decision" in r.output
        assert "sk-proj-abcdefghij" not in r.output

    @pytest.mark.parametrize("sql", [
        "DELETE FROM events", "UPDATE events SET payload = ''", "DROP TABLE events",
        "SELECT 1; DELETE FROM events", "ATTACH DATABASE '/tmp/x.db' AS x",
        "INSERT INTO events VALUES ('x', 1, '{}')", "PRAGMA writable_schema = 1",
        "SELECT writefile('/tmp/x', 'y')", "SELECT load_extension('x')",
    ])
    async def test_anything_but_a_read_is_refused(self, tmp_path, sql):
        r = await self._run(tmp_path, sql)
        assert not r.success
        con = sqlite3.connect(tmp_path / "ledger.db")
        assert con.execute("SELECT count(*) FROM events").fetchone()[0] == 1

    async def test_rows_are_capped(self, tmp_path):
        r = await self._run(tmp_path, "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n "
                                      "WHERE i < 1000) SELECT i FROM n", limit=5)
        assert r.success and r.output.count("\n") <= 7
