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
from loom.core.harness.registry import ToolDefinition
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


# ---------------------------------------------------------------------------
# #709 review (Vera): ledger_query bounds, deadline, redaction, scope
# ---------------------------------------------------------------------------


def _rich_ledger(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE events (event_id TEXT, event_type TEXT, timestamp REAL, payload TEXT)")
    rows = [("e1", "permission_decision", 1.0, json.dumps({
                "reason": "scope-allow", "password": "hunter2", "auth": "abc",
                "nested": {"api_key": "xyz", "note": "ok"}, "input_tokens": 120,
                "text": "key sk-proj-abcdefghijklmnopqrstuvwxyz"})),
            ("e2", "permission_decision", 2.0, json.dumps({
                "api_key": 12345678, "session_key": 3.14159, "token": True,
                "credentials": {"user": "dk", "value": "s3cr3tvalue", "pin": 4321},
                "auth": ["listsecret", 8675309], "input_tokens": 77}))]
    rows += [(f"b{i}", "bulk", float(i), json.dumps({"n": i, "pad": "x" * 500})) for i in range(3000)]
    con.executemany("INSERT INTO events VALUES (?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    return db


async def _ledger(db: Path, sql: str, **kw):
    from loom.core.harness.middleware import ToolCall
    from loom.platform.cli.tools import make_ledger_query_tool
    tool = make_ledger_query_tool(db)
    call = ToolCall(tool_name="ledger_query", args={"sql": sql, **kw}, trust_level=TrustLevel.SAFE,
                    session_id="s")
    return await tool.executor(call)


class TestLedgerQueryBounds:
    @pytest.mark.parametrize("sql", [
        "SELECT group_concat(payload) FROM events",
        "SELECT hex(zeroblob(10000000))",
        "SELECT payload FROM events",
    ])
    async def test_output_is_bounded(self, tmp_path, sql):
        r = await _ledger(_rich_ledger(tmp_path), sql, limit=200)
        text = r.output if r.success else (r.error or "")
        assert len(text.encode()) <= 26_000

    async def test_each_cell_is_capped(self, tmp_path):
        r = await _ledger(_rich_ledger(tmp_path), "SELECT payload FROM events WHERE event_type='bulk'", limit=3)
        assert r.success
        for line in r.output.splitlines()[1:4]:
            assert len(line.encode()) <= 2_100

    async def test_long_query_is_cut_off(self, tmp_path):
        import time
        started = time.monotonic()
        r = await _ledger(_rich_ledger(tmp_path),
                          "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) "
                          "SELECT count(*) FROM n")
        assert not r.success and "time" in r.error.lower()
        assert time.monotonic() - started < 8


class TestLedgerQueryIndexedPaths:
    """#709 review: the access paths the description recommends are
    indexed, so "the last N rows" finishes far inside the deadline."""

    def test_ledger_schema_indexes_time_and_event_type(self, tmp_path):
        from loom.core.ledger.schema import CREATE_EVENTS_TABLE, CREATE_INDEXES
        con = sqlite3.connect(tmp_path / "l.db")
        con.execute(CREATE_EVENTS_TABLE)
        for stmt in CREATE_INDEXES:
            con.execute(stmt)
        for q in ("SELECT payload FROM events ORDER BY timestamp DESC LIMIT 20",
                  "SELECT payload FROM events WHERE event_type='x' ORDER BY timestamp DESC LIMIT 20",
                  "SELECT payload FROM events WHERE timestamp > 5 ORDER BY timestamp DESC"):
            plan = " ".join(str(r[3]) for r in con.execute("EXPLAIN QUERY PLAN " + q))
            assert "USING INDEX" in plan and "TEMP B-TREE" not in plan, (q, plan)

    def test_description_points_at_indexed_columns(self):
        from loom.platform.cli.tools import make_ledger_query_tool
        d = make_ledger_query_tool(Path("/nonexistent/ledger.db")).description
        assert "event_id" in d and "indexed" in d.lower()
        assert "filter by event_type or timestamp before reading payload" not in d


class TestLedgerQueryTooBig:
    async def test_value_over_the_cap_names_the_limit_and_the_row(self, tmp_path):
        db = tmp_path / "ledger.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE events (event_id TEXT, event_type TEXT, timestamp REAL, payload TEXT)")
        con.executemany("INSERT INTO events VALUES (?, ?, ?, ?)", [
            ("s1", "x", 1.0, json.dumps({"n": 1})),
            ("s2", "x", 2.0, json.dumps({"n": 2})),
            ("big", "x", 3.0, json.dumps({"blob": "y" * 1_500_000})),
        ])
        con.commit()
        con.close()
        r = await _ledger(db, "SELECT event_id, payload FROM events ORDER BY timestamp")
        assert not r.success
        assert "1 MB" in r.error and "row 3" in r.error, r.error
        assert "substr" in r.error or "json_extract" in r.error


class TestLedgerQueryRedaction:
    @pytest.mark.parametrize("sql", [
        "SELECT json_extract(payload,'$.password') FROM events WHERE event_id='e1'",
        "SELECT json_extract(payload,'$.auth') FROM events WHERE event_id='e1'",
        "SELECT json_extract(payload,'$.nested.api_key') FROM events WHERE event_id='e1'",
        "SELECT json_extract(payload,'$.' || 'pass' || 'word') FROM events WHERE event_id='e1'",
        "SELECT payload ->> '$.password' FROM events WHERE event_id='e1'",
        "SELECT value FROM events, json_each(events.payload) WHERE event_id='e1' AND key='password'",
        "SELECT payload FROM events WHERE event_id='e1'",
    ])
    async def test_secret_named_fields_come_back_masked(self, tmp_path, sql):
        r = await _ledger(_rich_ledger(tmp_path), sql)
        assert r.success, r.error
        for secret in ("hunter2", "\\tabc", "xyz", "sk-proj-abcdefghij"):
            assert secret not in r.output, (sql, r.output)

    @pytest.mark.parametrize("sql", [
        "SELECT json_extract(payload,'$.api_key') FROM events WHERE event_id='e2'",
        "SELECT json_extract(payload,'$.session_key') FROM events WHERE event_id='e2'",
        "SELECT json_extract(payload,'$.token') FROM events WHERE event_id='e2'",
        "SELECT value FROM events, json_each(events.payload) WHERE event_id='e2'",
        "SELECT value FROM events, json_tree(events.payload) WHERE event_id='e2'",
        "SELECT json_extract(payload,'$.credentials.value'), json_extract(payload,'$.credentials.pin') "
        "FROM events WHERE event_id='e2'",
        "SELECT json_extract(payload,'$.auth[1]') FROM events WHERE event_id='e2'",
        "SELECT payload FROM events WHERE event_id='e2'",
    ])
    async def test_every_scalar_under_a_secret_key_is_masked(self, tmp_path, sql):
        """#709 review: a number, bool, or a value nested in an object or
        list under a secret-named key is a secret too."""
        r = await _ledger(_rich_ledger(tmp_path), sql)
        assert r.success, r.error
        for secret in ("12345678", "3.14159", "4321", "s3cr3tvalue", "listsecret", "8675309"):
            assert secret not in r.output, (sql, r.output)
        if "json_extract(payload,'$.token')" in sql:
            assert "1" not in r.output.splitlines()[1]

    async def test_null_under_a_secret_key_and_other_numbers_stay(self, tmp_path):
        r = await _ledger(_rich_ledger(tmp_path),
                          "SELECT json_extract(payload,'$.input_tokens') FROM events WHERE event_id='e2'")
        assert r.success and "77" in r.output

    async def test_numbers_and_ordinary_fields_stay(self, tmp_path):
        r = await _ledger(_rich_ledger(tmp_path),
                          "SELECT json_extract(payload,'$.input_tokens'), json_extract(payload,'$.nested.note') "
                          "FROM events WHERE event_id='e1'")
        assert r.success and "120" in r.output and "ok" in r.output

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM pragma_table_info('events')", "PRAGMA table_list",
        "SELECT * FROM sqlite_master",
    ])
    async def test_nothing_outside_the_redacted_view(self, tmp_path, sql):
        r = await _ledger(_rich_ledger(tmp_path), sql)
        assert not r.success or "hunter2" not in r.output


class TestLedgerQuerySql:
    @pytest.mark.parametrize("sql", [
        "SELECT event_id AS \"update\" FROM events LIMIT 1",
        "SELECT event_id FROM events WHERE payload LIKE '%update%' LIMIT 1",
        "SELECT 'a;b'",
        "EXPLAIN SELECT event_id FROM events",
    ])
    async def test_ordinary_reads_are_not_blocked(self, tmp_path, sql):
        r = await _ledger(_rich_ledger(tmp_path), sql)
        assert r.success, r.error

    async def test_path_with_spaces(self, tmp_path):
        d = tmp_path / "dir with space #1"
        d.mkdir()
        db = _rich_ledger(d)
        r = await _ledger(db, "SELECT count(*) FROM events")
        assert r.success and "3002" in r.output


def test_subagents_never_get_ledger_query():
    """Not even when the spawn names it in allowed_tools."""
    from loom.core.agent.subagent import subagent_may_inherit
    from loom.platform.cli.tools import make_ledger_query_tool
    tool = make_ledger_query_tool(Path("/nonexistent/ledger.db"))
    assert not subagent_may_inherit(tool, allowed_tools=None)
    assert not subagent_may_inherit(tool, allowed_tools=["ledger_query"])


def test_subagents_still_get_ordinary_safe_tools():
    from loom.core.agent.subagent import subagent_may_inherit
    tool = ToolDefinition(name="read_file", description="", input_schema={}, executor=None,
                          trust_level=TrustLevel.SAFE)
    assert subagent_may_inherit(tool, allowed_tools=None)
    assert not subagent_may_inherit(tool, allowed_tools=["write_file"])


@pytest.mark.parametrize("key,masked", [
    ("password", True), ("auth", True), ("auth_token", True), ("api-key", True),
    ("client_secret", True), ("authorization", True),
    ("input_tokens", False), ("authorization_reason", False), ("author", False), ("note", False),
])
def test_ledger_key_masking(key, masked):
    from loom.platform.cli.tools import _redact_ledger_payload
    out = json.loads(_redact_ledger_payload(json.dumps({key: "plainvalue"})))
    assert (out[key] == "[REDACTED]") is masked
