"""#650 S1 PR-B contract: ``recall(ref=...)`` is an honest identity read.

Invariants under test (spec docs/designs/61 r2.2 §1.2, §4.2, §6.1):

- a ref is read by identity — no fuzzy search runs, an unknown or malformed
  ref says so;
- ``log:{session}:{turn}`` expands that turn plus the turn before and after
  (≤3 turns, ≤128 rows, ≤8,000 chars), and every cut is marked;
- a tool row is filled from ``action_records.output_prefix`` joined on
  session + tool_call_id, with its action state; a capture that was itself
  truncated says so; an unjoined row says it is only the stored preview;
- a session that is gone is reported as gone, a missing turn as missing —
  never as "it never happened";
- ``mem:`` carries a value hash: a revised fact says it was revised, a merged
  key follows the redirect and says so; ``skill:`` carries its version;
- read-time secret redaction holds in expansions too;
- ``_log_message`` records ``ledger_turn_id``; turns without one say so.

All data is synthetic.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from loom.core.memory.procedural import ProceduralMemory, SkillGenome
from loom.core.memory.search import (
    REF_EXPAND_CHARS,
    MemorySearch,
    MemorySearchResult,
    value_hash,
)
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.store import SQLiteStore


async def _store(tmp_path) -> SQLiteStore:
    store = SQLiteStore(str(tmp_path / "memory.db"))
    await store.initialize()
    return store


async def _log(db, session_id, turn, role, content, *, metadata=None,
               created_at="2026-02-01T00:00:00+00:00", raw_json=None):
    await db.execute(
        "INSERT INTO session_log (session_id, turn_index, role, content, raw_json, metadata, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (session_id, turn, role, content, raw_json, json.dumps(metadata or {}), created_at),
    )
    await db.commit()


async def _action(db, *, session_id, call_id, output, output_len=None,
                  final_state="memorialized", intent="post the briefing"):
    await db.execute(
        "INSERT INTO action_records (id, envelope_id, session_id, turn_index, tool_name, "
        "call_id, final_state, intent_summary, created_at, output_prefix, output_len) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"a-{call_id}", "e1", session_id, 0, "fetch", call_id, final_state, intent,
         "2026-02-01T00:00:00+00:00", output,
         output_len if output_len is not None else len(output)),
    )
    await db.commit()


def _search(db) -> MemorySearch:
    return MemorySearch(SemanticMemory(db), ProceduralMemory(db))


# ── log: ─────────────────────────────────────────────────────────────────────


async def test_log_ref_expands_the_turn_and_its_neighbours_only(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        for turn, word in [(1, "alpha"), (2, "bravo"), (4, "charlie"), (6, "delta"), (7, "echo")]:
            await _log(db, "s1", turn, "user", f"{word} question")
            await _log(db, "s1", turn, "assistant", f"{word} answer")
        out = await _search(db).expand("log:s1:4")
    assert out.found
    text = out.text
    # neighbours are the nearest existing turns, not turn_index ± 1
    for word in ("bravo", "charlie", "delta"):
        assert f"{word} question" in text and f"{word} answer" in text
    assert "alpha" not in text and "echo" not in text
    assert text.index("bravo") < text.index("charlie") < text.index("delta")


async def test_log_ref_never_falls_back_to_search(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "user", "the lantern hinge")
        out = await _search(db).expand("lantern hinge")
    assert not out.found
    assert "the lantern hinge" not in out.text  # no row came back
    assert "log:" in out.text  # names the accepted ref forms


async def test_tool_row_is_filled_from_action_records_with_its_state(tmp_path):
    full = "RESULT-START " + "x" * 2000 + " RESULT-END"
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "user", "fetch the page")
        await _log(db, "s1", 1, "tool", full[:500],
                   metadata={"tool_call_id": "call-1", "tool_name": "fetch"})
        await _action(db, session_id="s1", call_id="call-1", output=full)
        out = await _search(db).expand("log:s1:1")
    assert "RESULT-END" in out.text            # beyond the 500-char preview
    assert "memorialized" in out.text and "post the briefing" in out.text
    assert "preview" not in out.text.lower()


async def test_capture_truncation_and_unjoined_rows_are_labelled(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "tool", "short preview of a",
                   metadata={"tool_call_id": "call-a", "tool_name": "fetch"})
        await _action(db, session_id="s1", call_id="call-a", output="p" * 300, output_len=40_000)
        await _log(db, "s1", 1, "tool", "orphan preview",
                   metadata={"tool_call_id": "call-gone", "tool_name": "fetch"})
        out = await _search(db).expand("log:s1:1")
    assert "40,000" in out.text                # capture kept 300 of 40,000
    assert "orphan preview" in out.text
    assert "stored preview only" in out.text


async def test_expansion_respects_the_char_budget_and_marks_cuts(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "user", "before " + "b" * 9000)
        await _log(db, "s1", 2, "user", "TARGET " + "t" * 9000)
        await _log(db, "s1", 3, "user", "after " + "a" * 9000)
        out = await _search(db).expand("log:s1:2")
    assert len(out.text) <= REF_EXPAND_CHARS
    assert "TARGET" in out.text
    assert "cut" in out.text
    # the target turn is served first; neighbours take what is left
    assert out.text.count("t" * 100) > out.text.count("b" * 100) > 0


async def test_expansion_caps_rows_and_says_so(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        for i in range(200):
            await _log(db, "s1", 1, "assistant", f"r{i}")
        out = await _search(db).expand("log:s1:1")
    assert "r0" in out.text and "r127" in out.text and "r128" not in out.text
    assert "128" in out.text and "200" in out.text


async def test_gone_session_and_missing_turn_are_reported_not_denied(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "user", "x")
        await _log(db, "s1", 5, "user", "y")
        search = _search(db)
        gone = await search.expand("log:ghost:1")
        missing = await search.expand("log:s1:3")
    assert not gone.found and "no rows" in gone.text.lower()
    assert "never" not in gone.text.lower()
    assert not missing.found and "1" in missing.text and "5" in missing.text


async def test_expansion_keeps_read_time_secret_redaction(tmp_path):
    secret = "sk-ant-" + "a" * 40
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "user", f"key api_key={secret}")
        await _log(db, "s1", 1, "tool", "p", metadata={"tool_call_id": "c", "tool_name": "env"})
        await _action(db, session_id="s1", call_id="c", output=f"TOKEN={secret}")
        out = await _search(db).expand("log:s1:1")
    assert secret not in out.text


async def test_ledger_link_is_shown_or_its_absence_stated(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "user", "old turn")
        await _log(db, "s1", 2, "user", "new turn", metadata={"ledger_turn_id": "turn_abc123"})
        out = await _search(db).expand("log:s1:2")
    assert "turn_abc123" in out.text
    assert "no ledger link" in out.text


async def test_assistant_tool_calls_are_shown_by_name(tmp_path):
    raw = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "create_post", "arguments": json.dumps({"forum_channel_id": 0})}},
    ]}
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "assistant", "[tool_use]", raw_json=json.dumps(raw))
        out = await _search(db).expand("log:s1:1")
    assert "create_post" in out.text and "forum_channel_id" in out.text


# ── mem: / skill: ────────────────────────────────────────────────────────────


async def test_mem_ref_reads_by_key_and_flags_revision(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        semantic = SemanticMemory(db)
        await semantic.upsert(SemanticEntry(key="user:tea", value="prefers oolong"))
        old_hash = value_hash("prefers oolong")
        search = MemorySearch(semantic, ProceduralMemory(db))
        same = await search.expand(f"mem:user:tea@{old_hash}")
        await semantic.upsert(SemanticEntry(key="user:tea", value="prefers green tea now"))
        revised = await search.expand(f"mem:user:tea@{old_hash}")
        bare = await search.expand("mem:user:tea")
    assert same.found and "prefers oolong" in same.text and "revised" not in same.text
    assert revised.found and "prefers green tea now" in revised.text and "revised" in revised.text
    assert bare.found and "revised" not in bare.text


async def test_mem_ref_follows_a_consolidation_redirect(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        semantic = SemanticMemory(db)
        await semantic.upsert(SemanticEntry(key="k:survivor", value="merged truth"))
        await semantic.upsert(SemanticEntry(
            key="k:old", value="[merged]", metadata={"redirected_to": "k:survivor"},
        ))
        out = await MemorySearch(semantic, ProceduralMemory(db)).expand("mem:k:old")
    assert out.found and "merged truth" in out.text and "mem:k:survivor" in out.text


async def test_missing_mem_and_skill_refs_say_so(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        search = _search(db)
        mem = await search.expand("mem:nope")
        skill = await search.expand("skill:nope")
    assert not mem.found and "mem:nope" in mem.text
    assert not skill.found and "skill:nope" in skill.text


async def test_skill_ref_reads_body_and_flags_a_newer_version(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        procedural = ProceduralMemory(db)
        await procedural.upsert(SkillGenome(name="brew", body="steep 3 minutes", version=2))
        search = MemorySearch(SemanticMemory(db), procedural)
        current = await search.expand("skill:brew@v2")
        stale = await search.expand("skill:brew@v1")
    assert current.found and "steep 3 minutes" in current.text and "newer" not in current.text
    assert stale.found and "v2" in stale.text and "newer" in stale.text


def test_displayed_refs_carry_versions_but_identity_stays_stable():
    mem = MemorySearchResult(type="semantic", key="user:tea", value="oolong", score=1.0)
    skill = MemorySearchResult(type="skill", key="brew", value="b", score=1.0,
                               metadata={"version": 3})
    assert mem.ref == "mem:user:tea"
    assert f"mem:user:tea@{value_hash('oolong')}" in mem.format()
    assert skill.ref == "skill:brew" and "skill:brew@v3" in skill.format()


async def test_mem_expansion_records_access(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        semantic = SemanticMemory(db)
        await semantic.upsert(SemanticEntry(key="k:a", value="v"))
        await MemorySearch(semantic, ProceduralMemory(db)).expand("mem:k:a")
        cur = await db.execute("SELECT last_accessed_at FROM semantic_entries WHERE key='k:a'")
        (accessed,) = await cur.fetchone()
    assert accessed


# ── tool surface ─────────────────────────────────────────────────────────────


def _tool_call(args):
    from loom.core.harness.middleware import ToolCall
    from loom.core.harness.permissions import TrustLevel

    return ToolCall(tool_name="recall", args=args, trust_level=TrustLevel.SAFE, session_id="t")


async def test_tool_rejects_ref_with_query_and_names_ignored_filters(tmp_path):
    from loom.platform.cli.tools import make_recall_tool

    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "user", "the lantern hinge")
        search = _search(db)

        class _Facade:
            async def expand_ref(self, ref, mark_accessed=True):
                return await search.expand(ref, mark_accessed=mark_accessed)

        tool = make_recall_tool(_Facade())  # type: ignore[arg-type]
        both = await tool.executor(_tool_call({"ref": "log:s1:1", "query": "lantern"}))
        filtered = await tool.executor(_tool_call(
            {"ref": "log:s1:1", "query": "", "type": "all", "domain": "user", "since": ""},
        ))
    assert not both.success and "ref" in both.error and "query" in both.error
    assert filtered.success and "the lantern hinge" in filtered.output
    assert "domain" in filtered.output  # a filled filter is named as ignored
    assert "since" not in filtered.output.split("ignored", 1)[-1].split("\n", 1)[0]


def test_recall_tool_output_is_never_spilled():
    from loom.platform.cli.tools import make_recall_tool

    assert make_recall_tool(SimpleNamespace()).inline_only  # type: ignore[arg-type]


# ── ledger_turn_id ───────────────────────────────────────────────────────────


async def test_log_message_records_the_ledger_turn_id():
    from loom.core.ledger.correlation import reset_turn_id, set_turn_id
    from loom.core.session import LoomSession

    written: list[dict] = []

    class _Log:
        async def log_message(self, sid, ti, role, content, metadata, raw_json=None):
            written.append(metadata)

    session = LoomSession.__new__(LoomSession)
    session._session_log = _Log()
    session.session_id = "s"
    session._turn_index = 0

    await session._log_message("user", "hi", turn_index=0)
    token = set_turn_id("turn_feed")
    try:
        await session._log_message("user", "hi", {"k": 1}, turn_index=0)
    finally:
        reset_turn_id(token)
    assert "ledger_turn_id" not in written[0]
    assert written[1] == {"k": 1, "ledger_turn_id": "turn_feed"}


async def test_a_ref_shown_by_recall_reads_back_cleanly(tmp_path):
    """Round trip: what format() shows is what expand() accepts unchanged."""
    import re

    store = await _store(tmp_path)
    async with store.connect() as db:
        semantic = SemanticMemory(db)
        await semantic.upsert(SemanticEntry(key="user:lantern", value="the lantern hinge is brass"))
        await _log(db, "s1", 3, "assistant", "the lantern hinge cracked")
        search = MemorySearch(semantic, ProceduralMemory(db))
        shown = "\n".join(r.format() for r in await search.recall("lantern hinge"))
        refs = re.findall(r"\b(?:mem|log):\S+", shown)
        assert len(refs) == 2
        for ref in refs:
            out = await search.expand(ref)
            assert out.found and "revised" not in out.text, ref


async def test_a_tool_heavy_target_turn_leaves_room_for_its_neighbours(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "user", "BEFORE-QUESTION")
        for i in range(10):
            cid = f"c{i}"
            await _log(db, "s1", 2, "tool", "p", metadata={"tool_call_id": cid, "tool_name": "fetch"})
            await _action(db, session_id="s1", call_id=cid, output="o" * 5000)
        await _log(db, "s1", 3, "user", "AFTER-QUESTION")
        out = await _search(db).expand("log:s1:2")
    assert len(out.text) <= REF_EXPAND_CHARS
    assert "BEFORE-QUESTION" in out.text and "AFTER-QUESTION" in out.text


# ── #659 review (opencode, Loom Agent) ───────────────────────────────────────


async def test_a_target_turn_over_the_row_budget_leaves_rows_for_its_neighbours(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "user", "BEFORE-QUESTION", metadata={"ledger_turn_id": "turn_b"})
        for i in range(200):
            await _log(db, "s1", 2, "assistant", f"r{i}")
        await _log(db, "s1", 3, "user", "AFTER-QUESTION", metadata={"ledger_turn_id": "turn_a"})
        out = await _search(db).expand("log:s1:2")
    assert "BEFORE-QUESTION" in out.text and "AFTER-QUESTION" in out.text
    titles = {l.split(" ")[2]: l for l in out.text.splitlines() if l.startswith("── turn")}
    assert "turn_b" in titles["1"] and "turn_a" in titles["3"]


async def test_a_turn_with_no_rows_shown_makes_no_claim_about_them(tmp_path, monkeypatch):
    """With the reserves a neighbour always gets rows; force the defensive
    path (no reserve) and check its title claims nothing about unread rows."""
    import loom.core.memory.search as search_module

    monkeypatch.setattr(search_module, "_REF_NEIGHBOUR_ROWS", 0)
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "user", "x", metadata={"ledger_turn_id": "turn_b"})
        for i in range(200):
            await _log(db, "s1", 2, "assistant", f"r{i}")
        out = await _search(db).expand("log:s1:2")
    title = next(l for l in out.text.splitlines() if l.startswith("── turn 1"))
    assert "none shown" in title and "ledger" not in title


async def test_joined_action_without_a_capture_falls_back_to_the_preview(tmp_path):
    """Pre-#569 action rows have NULL output_prefix (32k rows live)."""
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "tool", "THE-STORED-PREVIEW",
                   metadata={"tool_call_id": "old", "tool_name": "fetch"})
        await db.execute(
            "INSERT INTO action_records (id, envelope_id, session_id, turn_index, tool_name, "
            "call_id, final_state, created_at) VALUES ('a','e','s1',1,'fetch','old','memorialized',"
            "'2026-01-01T00:00:00+00:00')"
        )
        await db.commit()
        out = await _search(db).expand("log:s1:1")
    assert "THE-STORED-PREVIEW" in out.text
    assert "memorialized" in out.text and "no captured output" in out.text


async def test_header_describes_only_the_neighbours_that_exist(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "only", 7, "user", "x")
        await _log(db, "s2", 1, "user", "x")
        await _log(db, "s2", 2, "user", "y")
        search = _search(db)
        alone = (await search.expand("log:only:7")).text.splitlines()[0]
        first = (await search.expand("log:s2:1")).text.splitlines()[0]
        last = (await search.expand("log:s2:2")).text.splitlines()[0]
    assert "only logged turn" in alone and "before" not in alone
    assert "after" in first and "before" not in first
    assert "before" in last and "turn after" not in last


def test_cut_marker_states_the_true_shown_count():
    import re

    from loom.core.memory.search import _cut

    out = _cut("z" * 10_000, 500)
    shown = int(re.search(r"cut: ([\d,]+) of", out)[1].replace(",", ""))
    assert len(out) <= 500 and out.count("z") == shown


async def test_long_tool_call_arguments_are_cut_visibly(tmp_path):
    raw = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "write", "arguments": json.dumps({"body": "q" * 1000})}},
    ]}
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "assistant", "[tool_use]", raw_json=json.dumps(raw))
        out = await _search(db).expand("log:s1:1")
    assert "cut:" in out.text


async def test_mem_and_skill_expansions_keep_secret_redaction(tmp_path):
    secret = "sk-ant-" + "a" * 40
    store = await _store(tmp_path)
    async with store.connect() as db:
        semantic, procedural = SemanticMemory(db), ProceduralMemory(db)
        await semantic.upsert(SemanticEntry(key="k:s", value=f"api_key={secret}"))
        await procedural.upsert(SkillGenome(name="sk", body=f"use api_key={secret}"))
        search = MemorySearch(semantic, procedural)
        mem = await search.expand("mem:k:s")
        skill = await search.expand("skill:sk")
    assert secret not in mem.text and secret not in skill.text


async def test_tool_reports_an_unresolved_ref_as_a_failed_read(tmp_path):
    from loom.platform.cli.tools import make_recall_tool

    store = await _store(tmp_path)
    async with store.connect() as db:
        search = _search(db)

        class _Facade:
            async def expand_ref(self, ref, mark_accessed=True):
                return await search.expand(ref, mark_accessed=mark_accessed)

        tool = make_recall_tool(_Facade())  # type: ignore[arg-type]
        res = await tool.executor(_tool_call({"ref": "mem:does:not:exist"}))
    assert not res.success and "mem:does:not:exist" in res.error
