"""#650 S4 — ``recall`` is the single memory entry.

Contracts:

* window mode: ``recall`` with ``since`` (and optional ``until``) but no
  ``query`` lists the sessions active in that window, newest first, and the
  memories updated in it — each with a ref that ``recall(ref=...)`` reads.
  Hard bounds, output budget, coverage line, sub-agent traces not listed as
  sessions. It never degrades into "the earliest N rows of the window", and
  without ``since`` there is still no recency fallback;
* ``recall_period`` and ``ledger_recall`` are gone from the agent's tools and
  from the code (rollback is a revert);
* no registered tool description, and no startup prompt text, names a tool
  that is not registered.

Synthetic sources only.
"""

import importlib
import json
import re
from pathlib import Path
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.core.memory.episodic import EpisodicMemory
from loom.core.memory.facade import MemoryFacade
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.search import RECALL_OUTPUT_CHARS, MemorySearch
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.session_log import SessionLog
from loom.core.memory.store import SQLiteStore
from loom.platform.cli.tools import make_recall_tool

BASE = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def mem(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    store = SQLiteStore(str(tmp_path / "memory.db"))
    await store.initialize()
    async with store.connect() as db:
        sem, proc = SemanticMemory(db), ProceduralMemory(db)
        yield MemoryFacade(
            semantic=sem, procedural=proc, episodic=EpisodicMemory(db),
            session_log=SessionLog(db), search=MemorySearch(sem, proc),
        )


async def _session(mem, sid, title, *, turns=10):
    db = mem.semantic._db
    await db.execute(
        "INSERT INTO sessions (session_id, model, title, started_at, last_active, turn_count) "
        "VALUES (?, 'm', ?, ?, ?, ?)",
        (sid, title, BASE.isoformat(), BASE.isoformat(), turns),
    )
    await db.commit()


async def _row(mem, sid, turn, role, content, at, metadata=None):
    db = mem.semantic._db
    await db.execute(
        "INSERT INTO session_log (session_id, turn_index, role, content, metadata, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sid, turn, role, content, json.dumps(metadata or {}), at.isoformat()),
    )
    await db.commit()


async def _fact(mem, key, value, at):
    await mem.semantic.upsert(SemanticEntry(key=key, value=value, source="memorize"))
    db = mem.semantic._db
    await db.execute(
        "UPDATE semantic_entries SET updated_at = ? WHERE key = ?", (at.isoformat(), key),
    )
    await db.commit()


def _call(args):
    return ToolCall(id="c", tool_name="recall", args=args,
                    trust_level=TrustLevel.SAFE, session_id="now")


async def _recall(mem, **args):
    return await make_recall_tool(mem).executor(_call(args))


def _h(hours):
    return BASE + timedelta(hours=hours)


# ── window mode ────────────────────────────────────────────────────────────


async def test_window_lists_sessions_newest_first_and_memories_with_refs(mem):
    await _session(mem, "older", "Morning reading")
    await _row(mem, "older", 3, "user", "read the essay on tide pools", _h(2))
    await _row(mem, "older", 3, "assistant", "done reading", _h(2.1))
    await _session(mem, "newer", "Evening chat")
    await _row(mem, "newer", 1, "user", "how was the walk", _h(8))
    await _row(mem, "newer", 2, "user", "and the tea", _h(9))
    await _fact(mem, "self:tide-pools", "tide pools are patient", _h(3))

    result = await _recall(mem, since=_h(0).isoformat(), until=_h(24).isoformat())

    assert result.success, result.error
    out = result.output
    # newest session first
    assert out.index("Evening chat") < out.index("Morning reading")
    # first user line in the window and a log ref to that turn
    assert "how was the walk" in out and "log:newer:1" in out
    assert "read the essay on tide pools" in out and "log:older:3" in out
    # turns active in the window
    assert re.search(r"2 turns", out)
    # memory updated in the window, readable by ref
    assert "mem:self:tide-pools@" in out and "tide pools are patient" in out
    assert "window" in out.splitlines()[-1].lower()  # coverage line names the mode


async def test_window_is_a_hard_bound_on_sessions_and_memories(mem):
    await _session(mem, "inside", "Inside")
    await _row(mem, "inside", 1, "user", "inside-line", _h(5))
    await _session(mem, "before", "Before")
    await _row(mem, "before", 1, "user", "before-line", _h(-5))
    # active inside the window and again long after it: listed by what
    # happened inside, never by its later activity
    await _session(mem, "spans", "Spans")
    await _row(mem, "spans", 4, "user", "spans-inside-line", _h(6))
    await _row(mem, "spans", 9, "user", "spans-later-line", _h(200))
    await _fact(mem, "fact-in", "in window", _h(4))
    await _fact(mem, "fact-out", "outside window", _h(100))

    out = (await _recall(mem, since=_h(0).isoformat(), until=_h(24).isoformat())).output

    assert "inside-line" in out and "spans-inside-line" in out
    assert "before-line" not in out and "Before" not in out
    assert "spans-later-line" not in out
    assert "fact-in" in out and "fact-out" not in out
    coverage = out.splitlines()[-1]
    assert "skills: excluded" in coverage


async def test_window_without_until_runs_to_now(mem):
    await _session(mem, "s", "Recent")
    await _row(mem, "s", 1, "user", "recent-line", datetime.now(UTC) - timedelta(hours=1))
    out = (await _recall(mem, since=(datetime.now(UTC) - timedelta(hours=48)).isoformat())).output
    assert "recent-line" in out


async def test_no_query_and_no_since_is_still_an_error_never_recent_rows(mem):
    await _session(mem, "s", "Recent")
    await _row(mem, "s", 1, "user", "recent-line", datetime.now(UTC))
    result = await _recall(mem)
    assert not result.success
    assert "recent-line" not in (result.error or "")
    only_until = await _recall(mem, until=_h(1).isoformat())
    assert not only_until.success and "since" in only_until.error
    backwards = await _recall(mem, since=_h(5).isoformat(), until=_h(1).isoformat())
    assert not backwards.success


async def test_empty_window_reports_nothing_found_without_fallback(mem):
    await _session(mem, "s", "Elsewhere")
    await _row(mem, "s", 1, "user", "elsewhere-line", _h(50))
    await _fact(mem, "elsewhere-fact", "x", _h(50))
    result = await _recall(mem, since=_h(0).isoformat(), until=_h(24).isoformat())
    assert result.success
    assert "elsewhere" not in result.output
    assert "does not mean" in result.output.lower() or "nothing" in result.output.lower()


async def test_window_does_not_regress_into_the_earliest_rows(mem):
    # An early, busy session fills the start of the window; the two newest
    # sessions are what a 'what happened lately' look-back needs to see.
    await _session(mem, "busy", "Busy early session")
    for i in range(60):
        await _row(mem, "busy", 1 + i // 3, "tool", f"busy-tool-output-{i}", _h(1 + i / 60))
    await _session(mem, "mid", "Midday")
    await _row(mem, "mid", 1, "user", "midday-line", _h(10))
    await _session(mem, "late", "Late night")
    await _row(mem, "late", 1, "user", "late-line", _h(20))

    out = (await _recall(mem, since=_h(0).isoformat(), until=_h(24).isoformat(), limit=2)).output

    assert "late-line" in out and "midday-line" in out
    assert "Busy early session" not in out
    assert "busy-tool-output" not in out
    assert "more" in out.splitlines()[-1].lower()  # the cut is reported


async def test_window_output_budget_and_access_marking(mem):
    # Memories alone overflow the budget: some are shown, the rest omitted.
    # Only the shown ones may be marked accessed (spec 61 §6.3).
    total = 10
    for i in range(total):
        await _fact(mem, f"fact-{i}-" + "k" * 150, "v" * 2_000, _h(i))

    result = await _recall(mem, since=_h(0).isoformat(), until=_h(24).isoformat(),
                           limit=total)

    assert len(result.output) <= RECALL_OUTPUT_CHARS
    shown = {m for m in re.findall(r"mem:(fact-\d+-k+)@", result.output)}
    assert 0 < len(shown) < total, len(shown)
    assert "omitted" in result.output
    db = mem.semantic._db
    cur = await db.execute(
        "SELECT key FROM semantic_entries WHERE last_accessed_at IS NOT NULL")
    accessed = {r[0] for r in await cur.fetchall()}
    assert accessed == shown


async def test_window_ref_round_trips_through_recall_ref(mem):
    await _session(mem, "s", "Walk")
    await _row(mem, "s", 2, "user", "shall we walk by the river", _h(3))
    await _row(mem, "s", 2, "assistant", "the river was bright", _h(3.1))
    out = (await _recall(mem, since=_h(0).isoformat(), until=_h(24).isoformat())).output
    ref = re.search(r"log:s:\d+", out).group(0)
    expanded = await _recall(mem, ref=ref)
    assert expanded.success, expanded.error
    assert "the river was bright" in expanded.output


async def test_window_does_not_list_sub_agent_traces_as_sessions(mem):
    await _session(mem, "parent", "Parent session")
    await _row(mem, "parent", 1, "user", "parent-line", _h(2))
    # child traces live in session_log under parent:child with no sessions row
    await _row(mem, "parent:child", 1, "user", "child-task-line", _h(1),
               metadata={"parent_session_id": "parent", "agent_id": "child"})
    out = (await _recall(mem, since=_h(0).isoformat(), until=_h(24).isoformat())).output
    assert "parent-line" in out
    assert "child-task-line" not in out
    assert "log:parent:child" not in out
    assert "sub-agent" in out.splitlines()[-1].lower()


async def test_window_type_semantic_lists_memories_only(mem):
    await _session(mem, "s", "Chat")
    await _row(mem, "s", 1, "user", "chat-line", _h(2))
    await _fact(mem, "fact-in", "in window", _h(3))
    out = (await _recall(mem, since=_h(0).isoformat(), until=_h(24).isoformat(),
                         type="semantic")).output
    assert "fact-in" in out and "chat-line" not in out


async def test_window_type_skill_is_refused_with_a_reason(mem):
    result = await _recall(mem, since=_h(0).isoformat(), type="skill")
    assert not result.success and "skill" in result.error.lower()


async def test_window_redacts_secrets_in_first_lines(mem):
    secret = "synthetic-secret-1234567890"
    await _session(mem, "s", "Keys")
    await _row(mem, "s", 1, "user", f"password={secret}", _h(1))
    out = (await _recall(mem, since=_h(0).isoformat(), until=_h(24).isoformat())).output
    assert secret not in out


async def test_window_session_header_is_one_line_even_for_raw_titles(mem):
    await _session(mem, "auto", "[[2026-11-18 16:00]]\n[Autonomous trigger: x] " + "y" * 300)
    await _row(mem, "auto", 1, "user", "chime body", _h(2))
    out = (await _recall(mem, since=_h(0).isoformat(), until=_h(24).isoformat())).output
    header = next(line for line in out.splitlines() if line.startswith("[session]"))
    assert "log:auto:1" in header and "turn in window" in header
    assert len(header) < 200


def test_recall_description_teaches_the_window_mode():
    desc = make_recall_tool(MagicMock()).description
    assert "since" in desc and "without query" in desc.lower()


# ── retired tools are gone ─────────────────────────────────────────────────


def test_retired_recall_tool_code_is_gone():
    from loom.core.memory import facade
    from loom.platform.cli import tools

    assert not hasattr(tools, "make_recall_period_tool")
    assert not hasattr(tools, "make_ledger_recall_tool")
    assert not hasattr(facade.MemoryFacade, "recall_period")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("loom.core.ledger.recall")


# A mention that is true although the tool is not in this registry:
# result_write is registered in every sub-agent's own registry, which is
# what spawn_agent describes. Conditionally registered tools (e.g. a web
# search that needs a key) are not allowed here: text the model always sees
# must not name them.
ALLOWED_MENTIONS = {("spawn_agent", "result_write")}

AGENT_TEMPLATE = Path(__file__).resolve().parents[1] / "Agent.md.example"
# A line only the tracked template has — proves it reached the prompt.
AGENT_TEMPLATE_MARKER = "## 長期追蹤（Pursuit）"

RETIRED_TOOL_NAMES = {
    "recall_period", "ledger_recall", "query_relations", "predict",
    "compress_session", "self_reflection",
}


@pytest_asyncio.fixture
async def started_session(monkeypatch, tmp_path):
    from loom.core import session as core_session
    from loom.core.session import LoomSession

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Hermetic: no repo-local SOUL.md / Agent.md reaches the prompt; the
    # tracked Agent.md.example is loaded as the agent template instead.
    monkeypatch.chdir(workspace)
    agent_md = workspace / "Agent.md"
    agent_md.write_text(AGENT_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(core_session, "build_router", lambda *a, **k: MagicMock())
    # Optional memory tools on, so their descriptions are checked too.
    monkeypatch.setattr(core_session, "_load_loom_config", lambda: {
        "identity": {"agent": str(agent_md)},
        "memory": {"attention": {"enabled": True}},
    })
    monkeypatch.setattr(core_session, "_load_env", lambda project_root=None: {})
    monkeypatch.setattr(core_session, "build_embedding_provider", lambda env, cfg: None)
    from rich.prompt import Confirm

    monkeypatch.setattr(Confirm, "ask", lambda *args, **kwargs: True)
    session = LoomSession(model="gpt-test", db_path=str(tmp_path / "loom.db"),
                          workspace=workspace)
    await session.start()
    try:
        yield session
    finally:
        await session.stop()


async def test_default_registry_has_recall_and_no_retired_recall_tools(started_session):
    reg = started_session.registry
    assert reg.get("recall") is not None
    assert reg.get("memorize") is not None
    assert reg.get("recall_period") is None
    assert reg.get("ledger_recall") is None


def _schema_text(tool) -> str:
    return tool.description + "\n" + json.dumps(tool.input_schema, ensure_ascii=False)


def _defined_tool_names() -> set[str]:
    """Every tool name the code base can register (static scan)."""
    from pathlib import Path

    import loom

    root = Path(loom.__file__).parent
    names: set[str] = set()
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "ToolDefinition(" not in text:
            continue
        names.update(re.findall(r"\bname\s*=\s*\"([a-z][a-z0-9_]+)\"", text))
    return names


async def test_no_tool_description_or_startup_prompt_names_an_unregistered_tool(started_session):
    reg = started_session.registry
    registered = {t.name for t in reg.list()}
    universe = (_defined_tool_names() | RETIRED_TOOL_NAMES) - registered
    texts = {t.name: _schema_text(t) for t in reg.list()}
    texts["<system prompt>"] = "\n".join(
        m["content"] for m in started_session.messages
        if m["role"] == "system" and isinstance(m.get("content"), str)
    )
    assert AGENT_TEMPLATE_MARKER in texts["<system prompt>"]
    offenders = {
        where: sorted(
            n for n in universe
            if (where, n) not in ALLOWED_MENTIONS
            and re.search(rf"(?<![\w.]){n}(?![\w])", text)
        )
        for where, text in texts.items()
    }
    offenders = {k: v for k, v in offenders.items() if v}
    assert offenders == {}


def test_memory_index_text_names_only_memorize_and_recall():
    """The index injected at startup teaches one line, not a backend choice."""
    from loom.core.memory.index import MemoryIndex

    text = MemoryIndex(semantic_count=3, skill_count=1, relational_count=2,
                       relational_predicates=["prefers"], anti_pattern_count=1).render()
    named = set(re.findall(r"\b(recall|memorize|relate|recall_period|ledger_recall|"
                           r"query_relations)\(", text))
    named |= set(re.findall(r"\b(recall_period|ledger_recall|query_relations|relate)\b", text))
    assert named <= {"recall", "memorize"}
    assert "memorize" in text and "recall" in text
