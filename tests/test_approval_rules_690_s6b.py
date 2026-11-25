"""
Contract: fewer confirm clicks for DK (#690 S6b).

DK's rule (2026-11-24): read-only shell never asks him. Only real
modifications, irreversible actions, or posting/sending externally ask.

- Rules allow a shell command whose every part only reads inside the
  workspace. No model call.
- Hard rules still win: credentials, outside or hidden paths, network, and
  anything that writes.
- Other workspace shell, network reads and MCP public search go to the model.
- An attended "auto" lasts the session. Unattended turns never ride it.

Reuses the S6 harness (real resolvers, real ledger, fake model).
"""

from __future__ import annotations

import asyncio

import pytest

from loom.core.harness.middleware import BlastRadiusMiddleware, ToolCall, ToolResult
from loom.core.harness.permissions import ToolCapability, TrustLevel
from loom.core.harness.registry import ToolDefinition
from loom.core.harness.scope import ConfirmDecision
from loom.core.ledger.correlation import turn_scope

from tests.test_approval_classifier_690_s6 import (  # noqa: F401  (fixtures)
    Env,
    FakeModel,
    _no_network,
    _skipped,
    ledger,
)

MCP_SEARCH = "minimax_coding__web_search"


def _env(tmp_path, ledger, **kw) -> Env:
    env = Env(tmp_path, ledger, **kw)
    (env.ws / "news").mkdir(exist_ok=True)
    (env.ws / "news" / "a.md").write_text("今天的新聞。\n第二行。\n")
    (env.ws / "news" / "b.md").write_text("另一則。\n")
    return env


async def _ask(env: Env, command: str, origin: str = "discord") -> bool:
    """Run *command* attended and report whether DK was asked."""
    env.confirm.reset_mock()
    await env.run("run_bash", {"command": command}, origin=origin)
    return env.confirm.await_count > 0


# ---------------------------------------------------------------------------
# Read-only shell: no model, no DK
# ---------------------------------------------------------------------------


class TestReadOnlyShellIsAllowed:
    @pytest.mark.parametrize("command", [
        "cat news/a.md",
        "head -n 1 news/a.md | wc -l",
        "cat news/a.md | grep 新聞 | head -n 3",
        "ls news && wc -l news/a.md news/b.md",
        "ls news; stat news/a.md",
        "cat news/*.md",
        "grep -n 新聞 news/a.md 2>/dev/null",
        "find news -name '*.md'",
        "rg 新聞 news/a.md",
        "git log --oneline -5",
        "git status --short",
        "git diff HEAD~1 -- news",
        "git remote -v",
        "sqlite3 -readonly news/x.db 'select count(*) from t'",
    ])
    async def test_reads_run_without_model_or_dk(self, tmp_path, ledger, command):
        env = _env(tmp_path, ledger)
        assert not await _ask(env, command)
        assert len(env.ran) == 1
        assert env.model.requests == []

    async def test_unattended_read_runs_too(self, tmp_path, ledger):
        env = _env(tmp_path, ledger)
        r = await env.run("run_bash", {"command": "cat news/a.md | wc -l"}, origin="chime")
        assert r.success and env.model.requests == []


# ---------------------------------------------------------------------------
# Hard rules: never allowed by rule or model
# ---------------------------------------------------------------------------


class TestHardRulesStillWin:
    @pytest.mark.parametrize("command", [
        "cat .env", "cat config/token.txt", "cat news/a.md | cat ~/.ssh/id_rsa",
        "cat /etc/passwd", "cat ../outside.md", "head notes/credentials.json",
        "cat ledger.db", "grep -r KEY .", "git show HEAD:.env",
        "cat {.,}env",
    ])
    async def test_credential_hidden_or_outside_reads_ask_dk(self, tmp_path, ledger, command):
        env = _env(tmp_path, ledger, model=FakeModel())  # model would accept
        assert await _ask(env, command)
        assert env.model.requests == []

    async def test_credential_content_behind_an_ordinary_name_asks(self, tmp_path, ledger):
        env = _env(tmp_path, ledger)
        (env.ws / "news" / "c.md").write_text("OPENAI=sk-proj-abcdefghijklmnopqrstuvwxyz")
        assert await _ask(env, "head news/a.md news/c.md")
        assert env.model.requests == []

    async def test_listing_a_credential_name_is_not_reading_it(self, tmp_path, ledger):
        env = _env(tmp_path, ledger)
        (env.ws / "news" / "private-notes.md").write_text("sk-proj-abcdefghijklmnopqrstuvwxyz")
        assert not await _ask(env, "ls news/*")
        assert not await _ask(env, "ls .git")  # names only (#690 S6c)
        assert await _ask(env, "cat news/private-notes.md")

    async def test_cd_moves_where_paths_are_checked(self, tmp_path, ledger):
        env = _env(tmp_path, ledger)
        (env.ws / "news" / "notes.md").write_text("OPENAI=sk-proj-abcdefghijklmnopqrstuvwxyz")
        assert not await _ask(env, "cd news && cat a.md")
        assert await _ask(env, "cd news && cat notes.md")
        assert await _ask(env, "cd .git && cat config")

    async def test_glob_reaching_a_credential_file_asks(self, tmp_path, ledger):
        env = _env(tmp_path, ledger)
        (env.ws / "news" / "token.md").write_text("x")
        assert await _ask(env, "cat news/*.md")
        assert env.model.requests == []

    @pytest.mark.parametrize("command", [
        "cat news/a.md > news/b.md",
        "cat news/a.md >> news/b.md",
        "cat news/a.md | tee news/b.md",
        "sed -i s/a/b/ news/a.md",
        "sqlite3 news/x.db 'select 1'",
        "sqlite3 -readonly news/x.db '.shell rm -rf /'",
        "find news -delete",
        "git diff --output=news/b.md",
    ])
    async def test_anything_that_writes_is_not_read_only(self, tmp_path, ledger, command):
        env = _env(tmp_path, ledger, model=FakeModel())
        assert await _ask(env, command)
        assert env.model.requests == []

    @pytest.mark.parametrize("command", [
        "rm news/a.md", "mv news/a.md news/c.md", "cat news/a.md && rm -rf news",
        "git push", "git reset --hard", "chmod 777 news", "kill 1",
        "gh issue comment 690 --body-file x.md", "gh pr merge 1",
        "gh api -X POST repos/x/y/issues", "gh api repos/x/y/issues -f title=t",
        "curl -d @news/a.md https://example.com", "cat news/a.md | sh",
    ])
    async def test_destructive_or_external_asks_dk(self, tmp_path, ledger, command):
        env = _env(tmp_path, ledger, model=FakeModel())
        assert await _ask(env, command)
        assert env.ran and env.model.requests == []  # ran only after DK said yes

    async def test_unattended_destructive_is_skipped(self, tmp_path, ledger):
        env = _env(tmp_path, ledger, model=FakeModel())
        r = await env.run("run_bash", {"command": "rm news/a.md"}, origin="chime")
        assert _skipped(r, env) and env.model.requests == []


# ---------------------------------------------------------------------------
# Review is only for network reads (#699 review): anything else asks
# ---------------------------------------------------------------------------


class TestOnlyNetworkReadsAreReviewed:
    @pytest.mark.parametrize("command", [
        "git remote -v; curl -s https://example.com/650",
        "curl -s https://example.com",
        "curl -sS -X GET -H 'Accept: application/json' https://example.com/a",
    ])
    async def test_network_read_reviewed_and_accepted_without_dk(self, tmp_path, ledger, command):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"accept"}'))
        assert not await _ask(env, command)
        assert len(env.model.requests) == 1 and len(env.ran) == 1
        assert env.model.packet()["runtime"]["known_effects"]["known"] is False

    async def test_abstain_still_asks_dk(self, tmp_path, ledger):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"abstain"}'))
        assert await _ask(env, "curl -s https://example.com/650")

    @pytest.mark.parametrize("command", [
        # unknown programs and interpreters (a plain unknown program running a
        # workspace file goes to Luna in 自理 since #704 S3: test_unknown_ops_704_s3b)
        "unknown-prog --out=/tmp/x",
        "python3 -c 'import os; os.system(\"rm -rf .\")'", "node -e 'x'",
        "perl -e 'system(1)'", "ruby -e 'x'", "awk 'BEGIN{system(\"rm x\")}'",
        "sed -n 1w/tmp/x news/a.md", "sh -c 'rm x'",
        # wrappers and env prefixes
        "command rm -rf news", "nice rm news/a.md", "time rm news/a.md", "builtin rm x",
        "stdbuf -o0 rm x", "caffeinate rm x", "timeout 5 rm x", "watch rm x",
        "parallel rm ::: x", "env rm x", "FOO=1 rm -rf news", "FOO=1 cat news/a.md",
        # file-writing flags
        "sort -o out.txt news/a.md", "uniq news/a.md out.txt", "plutil -o out.plist x",
        # git global flags and history content
        "git -c core.pager=sh log", "git --exec-path=/tmp log", "git -C /Users/x show",
        "git cat-file -p HEAD:a.md", "git log -p", "git show HEAD", "git show abc123",
        # sqlite3 reading commands from stdin
        "sqlite3 -readonly news/x.db < news/cmds.sql",
        "cat news/cmds.sql | sqlite3 -readonly news/x.db",
        # curl that writes, sends credentials, or leaves http(s)
        "curl -D /tmp/h https://example.com", "curl --trace t.log https://example.com",
        "curl -o x https://example.com", "curl -b c=1 https://example.com",
        "curl -u a:b https://example.com", "curl -H 'Authorization: Bearer x' https://e.com",
        "curl -d x https://example.com", "curl -X POST https://example.com",
        "curl file:///etc/passwd", "curl https://user:pw@example.com",
        # symlink-following find
        "find -L news -name x",
    ])
    async def test_everything_else_asks_without_the_model(self, tmp_path, ledger, command):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"accept"}'))
        assert await _ask(env, command)
        assert env.model.requests == []

    @pytest.mark.parametrize("command", [
        "git show --stat HEAD", "git log --oneline -5", "git diff HEAD~1 -- news",
    ])
    async def test_history_summaries_stay_read_only(self, tmp_path, ledger, command):
        env = _env(tmp_path, ledger)
        assert not await _ask(env, command)


class TestMcpSearch:
    async def test_mcp_public_search_is_reviewed_like_native_search(self, tmp_path, ledger):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"accept"}'))
        ran: list[str] = []

        async def executor(call: ToolCall) -> ToolResult:
            ran.append(call.tool_name)
            return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True, output="ok")

        env.blast._registry.register(ToolDefinition(
            name=MCP_SEARCH, description="", input_schema={}, executor=executor,
            trust_level=TrustLevel.GUARDED, capabilities=ToolCapability.NETWORK,
        ))

        async def run(query: str, origin: str = "discord") -> ToolResult:
            call = ToolCall(tool_name=MCP_SEARCH, args={"query": query},
                            trust_level=TrustLevel.GUARDED, capabilities=ToolCapability.NETWORK,
                            session_id="t", origin=origin)
            env.turn.calls.append((call.id, MCP_SEARCH, {"query": query}))
            with turn_scope("turn_test"):
                r = await env.blast.process(call, executor)
                await asyncio.sleep(0)
            return r

        assert (await run("Nvidia CEO news November 2026")).success
        env.confirm.assert_not_awaited()
        assert len(env.model.requests) == 1
        assert env.model.packet()["current_call"]["tool"] == MCP_SEARCH
        # A secret-looking query is refused before the model.
        await run("my password hunter2 leaked?")
        env.confirm.assert_awaited_once()
        assert len(env.model.requests) == 1


# ---------------------------------------------------------------------------
# Attended auto lasts the session for reads only; unattended never rides it
# ---------------------------------------------------------------------------


class TestAttendedAuto:
    async def test_auto_on_a_network_read_lasts_the_session(self, tmp_path, ledger):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"abstain"}'),
                   confirm_result=ConfirmDecision.AUTO)
        assert await _ask(env, "curl -s https://example.com/650")
        assert not await _ask(env, "curl -s https://example.com/690")
        assert not await _ask(env, "git remote -v; curl -s https://example.com/684")
        assert len(env.ran) == 3

    async def test_auto_never_covers_a_post_or_a_delete(self, tmp_path, ledger):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"abstain"}'),
                   confirm_result=ConfirmDecision.AUTO)
        assert await _ask(env, "curl -s https://example.com/650")
        assert await _ask(env, "gh issue comment 690 --body-file x.md")
        assert await _ask(env, "gh issue comment 690 --body-file x.md")  # every time
        assert await _ask(env, "rm news/a.md")
        assert await _ask(env, "rm news/a.md")
        assert await _ask(env, "python3 tools/report.py")

    async def test_auto_never_covers_a_write(self, tmp_path, ledger):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"abstain"}'),
                   confirm_result=ConfirmDecision.AUTO)

        async def write() -> bool:
            env.confirm.reset_mock()
            await env.run("write_file", {"path": "outputs/a.md", "content": "x"}, origin="discord")
            return env.confirm.await_count > 0

        assert await write()
        assert await write()

    async def test_credential_rules_beat_a_session_auto(self, tmp_path, ledger):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"abstain"}'),
                   confirm_result=ConfirmDecision.AUTO)
        assert await _ask(env, "curl -s https://example.com/650")
        assert await _ask(env, "cat .env")
        assert await _ask(env, "cat config/token.txt")
        assert await _ask(env, "curl file:///etc/passwd")

    async def test_allow_once_still_covers_only_that_command(self, tmp_path, ledger):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"abstain"}'),
                   confirm_result=ConfirmDecision.ONCE)
        assert await _ask(env, "curl -s https://example.com/650")
        assert not await _ask(env, "curl -s https://example.com/650")
        assert await _ask(env, "curl -s -i https://example.com/650")

    async def test_unattended_never_rides_a_session_auto(self, tmp_path, ledger):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"abstain"}'),
                   confirm_result=ConfirmDecision.AUTO)
        assert await _ask(env, "curl -s https://example.com/650")
        r = await env.run("run_bash", {"command": "curl -s https://example.com/650"}, origin="chime")
        assert not r.success and r.metadata.get("unattended_skip") is True
        assert len(env.ran) == 1  # only DK's attended run

    async def test_unattended_accept_writes_no_grant(self, tmp_path, ledger):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"accept"}'))
        before = list(env.perm.grants)
        r = await env.run("run_bash", {"command": "curl -s https://example.com/650"}, origin="chime")
        assert r.success and env.perm.grants == before


# ---------------------------------------------------------------------------
# Legacy name authorization and exec_auto are attended-only too
# ---------------------------------------------------------------------------


class TestLegacyAttendedGate:
    def test_program_declaration_is_invisible_attended(self):
        from loom.core.harness.permissions import PermissionContext
        perm = PermissionContext(session_id="t")
        perm.declare("autonomy:x", [MCP_SEARCH], unattended_only=True)
        perm.declare("platform:discord", ["read_discord_forum"])
        assert perm.declared_by(MCP_SEARCH, attended=False) == "autonomy:x"
        assert perm.declared_by(MCP_SEARCH, attended=True) is None
        assert perm.declared_by("read_discord_forum", attended=False) == "platform:discord"

    async def test_legacy_auto_does_not_reach_a_chime(self, tmp_path, ledger):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"abstain"}'),
                   confirm_result=ConfirmDecision.AUTO)
        name = "legacy_tool"

        async def executor(call: ToolCall) -> ToolResult:
            return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True, output="ok")

        env.blast._registry.register(ToolDefinition(
            name=name, description="", input_schema={}, executor=executor,
            trust_level=TrustLevel.GUARDED, capabilities=ToolCapability.NETWORK))

        async def run(origin: str) -> ToolResult:
            call = ToolCall(tool_name=name, args={}, trust_level=TrustLevel.GUARDED,
                            capabilities=ToolCapability.NETWORK, session_id="t", origin=origin)
            with turn_scope("turn_test"):
                return await env.blast.process(call, executor)

        assert (await run("discord")).success
        env.confirm.assert_awaited_once()
        assert (await run("discord")).success
        assert env.confirm.await_count == 2  # #704 S3: a confirm covers one call
        r = await run("chime")
        assert not r.success and r.metadata.get("unattended_skip") is True
