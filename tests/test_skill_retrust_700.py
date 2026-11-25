"""
Contract: a skill check module DK reviewed and approved is trusted (#700).

``skill_checks`` records a SHA-256 per check module and blocks ``load_skill``
when the file changes. When Loom Agent rewrites ``skills/<name>/<module>.py``
with ``write_file``, the confirm shows DK the unified diff against the trusted
version. If DK allows this one call after seeing it, the new bytes become the
trusted ones. Nothing else moves the trust record: a confirm that did not show
the diff, a lease or auto decision, a model accept, an unattended or bridged
call, bytes that differ from what DK saw, a symlink alias, a same-name copy
outside ``skills/``, or an edit outside the harness.

These tests drive ``BlastRadiusMiddleware`` with the real ``write_file`` tool,
a real ledger in tmp_path and the real integrity check (``_HASH_DIR`` is
isolated by conftest).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from loom.core.harness import skill_checks
from loom.core.harness.middleware import BlastRadiusMiddleware, ToolCall, ToolResult
from loom.core.harness.permissions import PermissionContext, TrustLevel
from loom.core.harness.registry import ToolRegistry
from loom.core.harness.scope import ConfirmDecision
from loom.core.harness.skill_checks import SkillCheckManager, SkillIntegrityError
from loom.core.ledger import LedgerEmitter, LedgerStore
from loom.core.ledger.correlation import turn_scope
from loom.platform.cli.tools import make_filesystem_tools

OLD = "async def guard(call):\n    return True\n"
NEW = "import shlex\n\n\nasync def guard(call):\n    return bool(shlex.split('a'))\n"
CHECKS = "skills/pet-cat/checks.py"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
async def ledger(tmp_path):
    store = LedgerStore(db_path=tmp_path / "ledger.db", blob_dir=tmp_path / "blobs")
    await store.open()
    yield store
    await store.close()


def showing(decision=ConfirmDecision.ONCE):
    """A platform confirm that shows DK the trust diff, then answers."""
    seen: list[str | None] = []

    async def confirm(call: ToolCall):
        diff = call.metadata.get("skill_trust_diff")
        seen.append(diff)
        if diff:
            call.metadata["skill_trust_diff_shown"] = True
        return decision
    confirm.seen = seen
    return confirm


def _make_skill(root: Path, name: str = "pet-cat", content: str = OLD) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
    (d / "checks.py").write_text(content)
    return d


class Env:
    def __init__(self, tmp_path: Path, ledger: LedgerStore, *, classifier=None,
                 name_authorized=(), tamper=None, confirm=None):
        self.ws = tmp_path / "ws"
        self.skill_dir = _make_skill(self.ws / "skills")
        # The first load records the trusted hash.
        SkillCheckManager.resolve_callable(self.skill_dir, "checks.guard")

        reg = ToolRegistry()
        for tool in make_filesystem_tools(self.ws):
            reg.register(tool)
        write_tool = reg.get("write_file")
        self.tool = write_tool
        real = write_tool.executor

        async def executor(call: ToolCall) -> ToolResult:
            result = await real(call)
            if tamper is not None:
                tamper(self.skill_dir / "checks.py")
            return result
        self.executor = executor

        self.perm = PermissionContext(session_id="t")
        for n in name_authorized:
            self.perm.authorize(n)
        self.emitter = LedgerEmitter(ledger, session_id="t")
        self.ledger = ledger
        self.confirm = confirm if confirm is not None else showing()
        self.confirm_mock = AsyncMock(side_effect=self.confirm)
        self.blast = BlastRadiusMiddleware(
            perm_ctx=self.perm, registry=reg, confirm_fn=self.confirm_mock,
            ledger_emitter=self.emitter, classifier=classifier, workspace=self.ws,
        )

    async def write(self, content: str = NEW, *, path: str = CHECKS,
                    origin: str = "interactive") -> ToolResult:
        call = ToolCall(
            tool_name="write_file",
            args={"path": path, "content": content, "justification": "S7 guard"},
            trust_level=TrustLevel.GUARDED, capabilities=self.tool.capabilities,
            session_id="t", origin=origin,
        )
        self.call = call
        with turn_scope("turn_test"):
            result = await self.blast.process(call, self.executor)
            await asyncio.sleep(0)
        return result

    def load(self, skill_dir: Path | None = None):
        return SkillCheckManager.resolve_callable(skill_dir or self.skill_dir, "checks.guard")

    def trusted(self) -> str:
        return skill_checks._trust_record(self.skill_dir / "checks.py").read_text().strip()

    async def trust_events(self) -> list:
        rows = await self.ledger.execute_sql(
            "SELECT payload FROM events WHERE event_type='skill_trust_update'"
        )
        return [json.loads(r[0]) for r in rows]


async def _assert_not_trusted(env: Env) -> None:
    assert env.trusted() == _sha(OLD)
    with pytest.raises(SkillIntegrityError):
        env.load()
    assert await env.trust_events() == []


# ---------------------------------------------------------------------------
# DK sees the diff and allows this one call
# ---------------------------------------------------------------------------


async def test_dk_approved_write_with_diff_is_trusted_and_loads(tmp_path, ledger):
    env = Env(tmp_path, ledger)
    r = await env.write()
    assert r.success
    env.confirm_mock.assert_awaited_once()
    assert env.call.metadata["approved_with_diff"] is True
    assert env.trusted() == _sha(NEW)
    assert callable(env.load())

    # The previous trust value is kept.
    backups = list((skill_checks._HASH_DIR / "backups").iterdir())
    assert [b.read_text().strip() for b in backups] == [_sha(OLD)]

    [payload] = await env.trust_events()
    assert payload["skill"] == "pet-cat"
    assert payload["file"] == str((env.skill_dir / "checks.py").resolve())
    assert payload["old_hash"] == _sha(OLD)[:12]
    assert payload["new_hash"] == _sha(NEW)[:12]
    assert payload["call_id"] == env.call.id


async def test_confirm_carries_diff_against_the_trusted_version(tmp_path, ledger):
    env = Env(tmp_path, ledger)
    await env.write()
    [diff] = env.confirm.seen
    assert "--- a/skills/pet-cat/checks.py" in diff
    assert "+++ b/skills/pet-cat/checks.py" in diff
    assert "-    return True" in diff
    assert "+import shlex" in diff


async def test_confirm_that_did_not_show_the_diff_is_not_trusted(tmp_path, ledger):
    env = Env(tmp_path, ledger, confirm=AsyncMock(return_value=ConfirmDecision.ONCE))
    r = await env.write()
    assert r.success
    assert "approved_with_diff" not in env.call.metadata
    await _assert_not_trusted(env)


@pytest.mark.parametrize("decision", [ConfirmDecision.AUTO, ConfirmDecision.SCOPE])
async def test_auto_or_lease_decision_is_not_trusted(tmp_path, ledger, decision):
    env = Env(tmp_path, ledger, confirm=showing(decision))
    r = await env.write()
    assert r.success
    await _assert_not_trusted(env)


async def test_write_covered_by_an_earlier_auto_is_not_trusted(tmp_path, ledger):
    env = Env(tmp_path, ledger, confirm=showing(ConfirmDecision.AUTO))
    await env.write("# first\n")
    env.confirm = showing()
    env.confirm_mock.side_effect = env.confirm
    r = await env.write()
    assert r.success
    assert env.call.metadata.get("approved_with_diff") is not True
    assert env.trusted() == _sha(OLD)


# ---------------------------------------------------------------------------
# Paths: only skills/<name>/<module>.py, never an alias or a copy
# ---------------------------------------------------------------------------


async def test_symlink_alias_is_not_trusted(tmp_path, ledger):
    env = Env(tmp_path, ledger)
    (env.skill_dir / "notes.py").symlink_to(env.skill_dir / "checks.py")
    r = await env.write(path="skills/pet-cat/notes.py")
    assert r.success
    assert (env.skill_dir / "checks.py").read_text() == NEW
    assert env.confirm.seen == [None]  # no trust offered
    await _assert_not_trusted(env)


async def test_same_name_backup_outside_skills_does_not_move_the_real_record(tmp_path, ledger):
    env = Env(tmp_path, ledger)
    backup = _make_skill(env.ws / "outputs" / "backup")
    SkillCheckManager.resolve_callable(backup, "checks.guard")
    r = await env.write(path="outputs/backup/pet-cat/checks.py")
    assert r.success
    assert env.confirm.seen == [None]
    assert env.trusted() == _sha(OLD)
    assert callable(env.load())
    with pytest.raises(SkillIntegrityError):
        env.load(backup)  # its own record is untouched too
    assert await env.trust_events() == []


def test_same_name_skill_dirs_keep_separate_records(tmp_path):
    a = _make_skill(tmp_path / "ws" / "skills")
    b = _make_skill(tmp_path / "home" / ".loom" / "skills", content=NEW)
    SkillCheckManager.resolve_callable(a, "checks.guard")
    SkillCheckManager.resolve_callable(b, "checks.guard")
    assert callable(SkillCheckManager.resolve_callable(a, "checks.guard"))
    assert callable(SkillCheckManager.resolve_callable(b, "checks.guard"))


def test_legacy_record_still_loads_and_migrates(tmp_path):
    d = _make_skill(tmp_path / "ws" / "skills")
    legacy = skill_checks._HASH_DIR / "pet-cat_checks.sha256"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(_sha(OLD))
    assert callable(SkillCheckManager.resolve_callable(d, "checks.guard"))
    assert skill_checks._trust_record(d / "checks.py") != legacy
    assert skill_checks._trust_record(d / "checks.py").read_text().strip() == _sha(OLD)


def test_legacy_record_mismatch_still_blocks(tmp_path):
    d = _make_skill(tmp_path / "ws" / "skills", content=NEW)
    legacy = skill_checks._HASH_DIR / "pet-cat_checks.sha256"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(_sha(OLD))
    with pytest.raises(SkillIntegrityError):
        SkillCheckManager.resolve_callable(d, "checks.guard")


# ---------------------------------------------------------------------------
# Paths that never had DK's reviewed consent
# ---------------------------------------------------------------------------


async def test_model_accepted_write_is_not_trusted(tmp_path, ledger):
    accept = SimpleNamespace(
        decide=AsyncMock(return_value=SimpleNamespace(approved=True, reason="model_accept")),
        auto_may_cover=lambda call: False,
        covers=lambda call: True,
    )
    env = Env(tmp_path, ledger, classifier=accept)
    r = await env.write()
    assert r.success
    env.confirm_mock.assert_not_awaited()
    await _assert_not_trusted(env)


async def test_unattended_bridged_write_is_not_trusted(tmp_path, ledger):
    # The autonomy daemon's allowed_tools bridge lets the write run with no
    # human attending.
    env = Env(tmp_path, ledger, name_authorized=("write_file",))
    r = await env.write(origin="autonomy")
    assert r.success
    assert (env.skill_dir / "checks.py").read_text() == NEW
    env.confirm_mock.assert_not_awaited()
    await _assert_not_trusted(env)


async def test_bytes_that_differ_from_the_approved_write_are_not_trusted(tmp_path, ledger):
    def tamper(path: Path) -> None:
        path.write_text(NEW + "import os\nos.system('true')\n")

    env = Env(tmp_path, ledger, tamper=tamper)
    r = await env.write()
    assert r.success
    env.confirm_mock.assert_awaited_once()
    await _assert_not_trusted(env)


async def test_out_of_band_edit_still_blocks(tmp_path, ledger):
    env = Env(tmp_path, ledger)
    await env.write()
    (env.skill_dir / "checks.py").write_text(NEW + "# edited outside Loom\n")
    with pytest.raises(SkillIntegrityError):
        env.load()
    assert env.trusted() == _sha(NEW)


async def test_untrusted_file_on_disk_gets_no_offer(tmp_path, ledger):
    # The file already differs from its record: a diff against it would show
    # DK the wrong baseline, so no trust is offered.
    env = Env(tmp_path, ledger)
    (env.skill_dir / "checks.py").write_text(OLD + "# drift\n")
    await env.write()
    assert env.confirm.seen == [None]
    assert env.trusted() == _sha(OLD)


# ---------------------------------------------------------------------------
# Platforms show the diff and say so
# ---------------------------------------------------------------------------


def _offer_call(diff: str) -> ToolCall:
    return ToolCall(
        tool_name="write_file",
        args={"path": CHECKS, "content": NEW, "justification": "S7 guard"},
        trust_level=TrustLevel.GUARDED, session_id="t", origin="interactive",
        metadata={"skill_trust_diff": diff},
    )


async def test_cli_confirm_prints_the_diff(monkeypatch):
    from rich.console import Console
    import loom.core.session as session_mod

    out = io.StringIO()
    monkeypatch.setattr(session_mod, "console", Console(file=out, width=200))
    app = SimpleNamespace(request_confirm=AsyncMock(return_value=ConfirmDecision.ONCE))
    fake = SimpleNamespace(_cancel_spinner_fn=None, _loom_app=app)
    diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-[red]old[/red]\n+new\n"
    call = _offer_call(diff)

    decision = await session_mod.LoomSession._confirm_tool_cli(fake, call)
    assert decision == ConfirmDecision.ONCE
    assert "-[red]old[/red]" in out.getvalue()  # printed verbatim, no markup
    assert "+new" in out.getvalue()
    assert call.metadata["skill_trust_diff_shown"] is True


async def _discord_confirm(monkeypatch, diff: str):
    import loom.platform.discord.bot as bot_mod

    class View:
        async def wait_decision(self):
            return ConfirmDecision.ONCE
    monkeypatch.setattr(bot_mod, "_ConfirmView", View)
    channel = SimpleNamespace(send=AsyncMock(return_value=object()))
    bot = object.__new__(bot_mod.LoomDiscordBot)
    bot._client = SimpleNamespace(get_channel=lambda _id: channel)
    bot._active_confirmations = {}
    call = _offer_call(diff)
    decision = await bot._make_confirm_fn(1)(call)
    assert decision == ConfirmDecision.ONCE
    return channel, call


async def test_discord_confirm_puts_a_short_diff_in_the_message(monkeypatch):
    diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
    channel, call = await _discord_confirm(monkeypatch, diff)
    [sent] = channel.send.await_args_list
    assert diff in sent.args[0]
    assert call.metadata["skill_trust_diff_shown"] is True


async def test_discord_confirm_attaches_a_long_diff(monkeypatch):
    diff = "--- a/x\n+++ b/x\n" + "".join(f"+line {i}\n" for i in range(400))
    channel, call = await _discord_confirm(monkeypatch, diff)
    [sent] = channel.send.await_args_list
    attached = sent.kwargs["file"]
    assert attached.fp.read().decode() == diff
    assert len(sent.args[0]) <= 2000
    assert call.metadata["skill_trust_diff_shown"] is True


async def test_discord_confirm_attaches_a_diff_holding_a_fence(monkeypatch):
    diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+s = '```'\n"
    channel, call = await _discord_confirm(monkeypatch, diff)
    [sent] = channel.send.await_args_list
    assert sent.kwargs["file"].fp.read().decode() == diff
    assert "```diff" not in sent.args[0]
