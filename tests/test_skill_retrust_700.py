"""
Contract: a skill check module DK approved in an attended turn is trusted (#700).

``skill_checks`` records a SHA-256 per check module and blocks ``load_skill``
when the file changes. When Loom Agent rewrites one with ``write_file`` and DK
confirms that write by hand, the confirm is the review, so the new bytes
become the trusted ones. Nothing else may move the trust record: a model
accept, an unattended or bridged call, bytes that differ from what DK saw,
or an edit outside the harness.

These tests drive ``BlastRadiusMiddleware`` with the real ``write_file`` tool,
a real ledger in tmp_path, and the real integrity check (``_HASH_DIR`` is
isolated by conftest).
"""

from __future__ import annotations

import asyncio
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


@pytest.fixture
async def ledger(tmp_path):
    store = LedgerStore(db_path=tmp_path / "ledger.db", blob_dir=tmp_path / "blobs")
    await store.open()
    yield store
    await store.close()


class Env:
    def __init__(self, tmp_path: Path, ledger: LedgerStore, *, classifier=None,
                 name_authorized=(), tamper=None):
        self.ws = tmp_path / "ws"
        self.skill_dir = self.ws / "skills" / "pet-cat"
        self.skill_dir.mkdir(parents=True)
        (self.skill_dir / "SKILL.md").write_text("---\nname: pet-cat\n---\n")
        (self.skill_dir / "checks.py").write_text(OLD)
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
        self.confirm = AsyncMock(return_value=ConfirmDecision.ONCE)
        self.blast = BlastRadiusMiddleware(
            perm_ctx=self.perm, registry=reg, confirm_fn=self.confirm,
            ledger_emitter=self.emitter, classifier=classifier,
        )

    async def write(self, content: str = NEW, *, origin: str = "interactive") -> ToolResult:
        call = ToolCall(
            tool_name="write_file",
            args={"path": CHECKS, "content": content, "justification": "S7 guard"},
            trust_level=TrustLevel.GUARDED, capabilities=self.tool.capabilities,
            session_id="t", origin=origin,
        )
        self.call = call
        with turn_scope("turn_test"):
            result = await self.blast.process(call, self.executor)
            await asyncio.sleep(0)
        return result

    def load(self):
        return SkillCheckManager.resolve_callable(self.skill_dir, "checks.guard")

    def trusted(self) -> str:
        return skill_checks._hash_path_for(self.skill_dir, "checks").read_text().strip()

    async def trust_events(self) -> list:
        rows = await self.ledger.execute_sql(
            "SELECT payload FROM events WHERE event_type='skill_trust_update'"
        )
        return [r[0] for r in rows]


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def test_dk_approved_attended_write_is_trusted_and_loads(tmp_path, ledger):
    env = Env(tmp_path, ledger)
    r = await env.write()
    assert r.success
    env.confirm.assert_awaited_once()
    assert env.trusted() == _sha(NEW)
    assert callable(env.load())

    # The previous trust value is kept.
    backups = list((skill_checks._HASH_DIR / "backups").iterdir())
    assert [b.read_text().strip() for b in backups] == [_sha(OLD)]

    import json
    [payload] = [json.loads(p) for p in await env.trust_events()]
    assert payload["skill"] == "pet-cat"
    assert payload["file"] == str(env.skill_dir / "checks.py")
    assert payload["old_hash"] == _sha(OLD)[:12]
    assert payload["new_hash"] == _sha(NEW)[:12]
    assert payload["call_id"] == env.call.id


async def test_model_accepted_write_is_not_trusted(tmp_path, ledger):
    accept = SimpleNamespace(
        decide=AsyncMock(return_value=SimpleNamespace(approved=True, reason="model_accept")),
        auto_may_cover=lambda call: False,
        covers=lambda call: True,
    )
    env = Env(tmp_path, ledger, classifier=accept)
    r = await env.write()
    assert r.success
    env.confirm.assert_not_awaited()
    assert env.trusted() == _sha(OLD)
    with pytest.raises(SkillIntegrityError):
        env.load()
    assert await env.trust_events() == []


async def test_unattended_bridged_write_is_not_trusted(tmp_path, ledger):
    # The autonomy daemon's allowed_tools bridge lets the write run with no
    # human attending.
    env = Env(tmp_path, ledger, name_authorized=("write_file",))
    r = await env.write(origin="autonomy")
    assert r.success
    assert (env.skill_dir / "checks.py").read_text() == NEW
    env.confirm.assert_not_awaited()
    assert env.trusted() == _sha(OLD)
    with pytest.raises(SkillIntegrityError):
        env.load()
    assert await env.trust_events() == []


async def test_bytes_that_differ_from_the_approved_write_are_not_trusted(tmp_path, ledger):
    def tamper(path: Path) -> None:
        path.write_text(NEW + "import os\nos.system('true')\n")

    env = Env(tmp_path, ledger, tamper=tamper)
    r = await env.write()
    assert r.success
    env.confirm.assert_awaited_once()
    assert env.trusted() == _sha(OLD)
    with pytest.raises(SkillIntegrityError):
        env.load()
    assert await env.trust_events() == []


async def test_out_of_band_edit_still_blocks(tmp_path, ledger):
    env = Env(tmp_path, ledger)
    await env.write()
    (env.skill_dir / "checks.py").write_text(NEW + "# edited outside Loom\n")
    with pytest.raises(SkillIntegrityError):
        env.load()
    assert env.trusted() == _sha(NEW)
