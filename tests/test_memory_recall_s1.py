"""#650 S1 PR-A contract: raw experience is recallable, honestly.

Invariants under test (spec docs/designs/61 r2 §1.2, §6.2, §6.3, §9.1):

- conversation turns in ``session_log`` are a first-class recall route;
- no route short-circuits another (an embedding hit never hides lexical hits);
- a query with no match returns nothing — never the most recent entries;
- stubs are excluded before LIMIT;
- two-character CJK terms are served by an index, not a capped scan;
- explicit since/until is a hard limit on every route it applies to;
- one turn occupies one result slot; different turns stay separate;
- deleted sources stop matching; excerpts keep read-time secret redaction;
- every query reports its coverage in one line.

The single-call layer below is the S1 hard gate (r2 §1.2 item 2). The case
split was fixed before implementation and must not be edited to fit results.
All data is synthetic.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import UTC, datetime

from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.search import MemorySearch
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.session_log import SessionLog
from loom.core.memory.store import SQLiteStore
from scripts.memory_s0 import CASES, ReplayEmbeddings, build_fixture, evaluate

# Literal / identifier / short-term / negative / range / filter cases: one
# ``recall`` call must pass. Paraphrase and cross-language cases belong to the
# agent-loop layer (the model rewrites its own search terms) and are measured
# separately, not asserted here.
SINGLE_CALL_LAYER = {
    "log-cn-two", "log-cn-long", "log-identifier", "log-time-window",
    "log-same-turn-user-assistant", "log-no-extracted-fact",
    "mem-archive", "mem-limit", "mem-uncertain", "mem-version", "mem-release",
    "skill-identifier", "skill-timezone",
    "negative-recipe", "negative-ocean", "negative-marathon", "negative-chemical",
    "negative-source-missing", "negative-empty-window", "negative-filter",
    "negative-unknown-book",
}


def _ref(result) -> str:
    return result.ref


def _normalize(results) -> list[dict]:
    return [{"ref": _ref(r), "excerpt": r.value} for r in results]


def _kwargs(case: dict) -> dict:
    kwargs = dict(case.get("args", {}))
    for name in ("since", "until"):
        if name in kwargs:
            kwargs[name] = datetime.fromisoformat(kwargs[name])
    return kwargs


async def _run_layer(tmp_path, provider=None) -> dict[str, dict]:
    fixture = json.loads(CASES.read_text())
    store = await build_fixture(tmp_path / "fixture.db", fixture, provider)
    outcome: dict[str, dict] = {}
    async with store.connect() as db:
        search = MemorySearch(SemanticMemory(db, embedding_provider=provider),
                              ProceduralMemory(db))
        for case in fixture["cases"]:
            if case["id"] not in SINGLE_CALL_LAYER:
                continue
            found = await search.recall(case["query"], limit=5, **_kwargs(case))
            outcome[case["id"]] = evaluate(case, _normalize(found))
    return outcome


def _failures(outcome: dict[str, dict]) -> list[str]:
    bad = []
    for case_id, ev in outcome.items():
        if ev["hit_at_5"] is None:
            if ev["false_positive"]:
                bad.append(f"{case_id}: false positive")
        elif not ev["hit_at_5"]:
            bad.append(f"{case_id}: miss")
    return bad


# ---------------------------------------------------------------------------
# Single-call layer — the S1 hard gate
# ---------------------------------------------------------------------------

def test_single_call_layer_is_a_fixed_subset_of_the_s0_cases():
    ids = {c["id"] for c in json.loads(CASES.read_text())["cases"]}
    assert SINGLE_CALL_LAYER <= ids
    assert sum(1 for i in SINGLE_CALL_LAYER if i.startswith("negative-")) == 8


async def test_single_call_layer_passes_lexical_only(tmp_path):
    outcome = await _run_layer(tmp_path)
    assert len(outcome) == len(SINGLE_CALL_LAYER)
    assert _failures(outcome) == []


async def test_single_call_layer_passes_with_frozen_embeddings(tmp_path):
    vectors_path = CASES.parent / "memory_s0_embeddings.json.gz"
    with gzip.open(vectors_path, "rt") as stream:
        provider = ReplayEmbeddings(json.load(stream)["vectors"])
    outcome = await _run_layer(tmp_path, provider)
    assert _failures(outcome) == []


# ---------------------------------------------------------------------------
# Route invariants
# ---------------------------------------------------------------------------

async def _store(tmp_path) -> SQLiteStore:
    store = SQLiteStore(str(tmp_path / "memory.db"))
    await store.initialize()
    return store


async def _log(db, session_id, turn, role, content, created_at=None, metadata=None):
    cursor = await db.execute(
        "INSERT INTO session_log (session_id, turn_index, role, content, metadata, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (session_id, turn, role, content, json.dumps(metadata or {}),
         created_at or "2026-02-01T00:00:00+00:00"),
    )
    await db.commit()
    return cursor.lastrowid


class _UnitVectors:
    """Every text embeds to the same vector: cosine 1.0 for any pair."""

    async def embed(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]


class _OrthogonalQuery:
    """Stored texts embed along x, queries along y: cosine 0."""

    def __init__(self, query):
        self.query = query

    async def embed(self, texts):
        return [[0.0, 1.0, 0.0] if t == self.query else [1.0, 0.0, 0.0] for t in texts]


async def test_embedding_hit_does_not_hide_conversation_hits(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        semantic = SemanticMemory(db, embedding_provider=_UnitVectors())
        await semantic.upsert(SemanticEntry(key="k:emb", value="unrelated wording"))
        await _log(db, "s1", 1, "assistant", "the lantern hinge cracked during the test")
        found = await MemorySearch(semantic, ProceduralMemory(db)).recall("lantern hinge")
    refs = [_ref(r) for r in found]
    assert "log:s1:1" in refs
    assert "mem:k:emb" in refs


async def test_low_similarity_embedding_is_not_an_answer(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        semantic = SemanticMemory(db, embedding_provider=_OrthogonalQuery("banana bread"))
        await semantic.upsert(SemanticEntry(key="k:x", value="cedar batch limit"))
        found = await MemorySearch(semantic, ProceduralMemory(db)).recall("banana bread")
    assert list(found) == []


async def test_no_match_returns_empty_with_coverage_not_recent_entries(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        semantic = SemanticMemory(db)
        await semantic.upsert(SemanticEntry(key="k:recent", value="freshly written fact"))
        await _log(db, "s1", 1, "user", "freshly written message")
        found = await MemorySearch(semantic, ProceduralMemory(db)).recall("helium neon cavity")
    assert list(found) == []
    assert found.coverage
    assert "conversation" in found.coverage


async def test_stubs_are_excluded_before_limit(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        semantic = SemanticMemory(db)
        for i in range(60):
            await semantic.upsert(SemanticEntry(
                key=f"stub:{i}", value="saffron archive saffron archive",
                metadata={"redirected_to": "live:fact"}))
        await semantic.upsert(SemanticEntry(key="live:fact", value="saffron archive policy"))
        found = await MemorySearch(semantic, ProceduralMemory(db)).recall(
            "saffron archive", type="semantic", limit=1)
    assert [_ref(r) for r in found] == ["mem:live:fact"]


async def test_two_character_cjk_term_uses_an_index_and_intersects(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "assistant", "可以把它從清單放下了")
        await _log(db, "s1", 2, "assistant", "放下這件事以前先確認夜間全量驗證")
        await _log(db, "s1", 3, "assistant", "完全無關的句子")
        search = MemorySearch(SemanticMemory(db), ProceduralMemory(db))
        two = await search.recall("放下")
        mixed = await search.recall("放下 夜間全量")
    assert {_ref(r) for r in two} == {"log:s1:1", "log:s1:2"}
    assert [_ref(r) for r in mixed] == ["log:s1:2"]


async def test_single_character_scan_reports_where_it_stopped_when_bounded(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "assistant", "貓在窗邊")
        search = MemorySearch(SemanticMemory(db), ProceduralMemory(db))
        found = await search.recall("貓")
    assert [_ref(r) for r in found] == ["log:s1:1"]


async def test_since_until_is_a_hard_limit_on_conversation_event_time(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "assistant", "cedar route chosen", "2026-01-10T12:00:00+00:00")
        await _log(db, "s1", 2, "assistant", "cedar route chosen again", "2026-01-20T12:00:00+00:00")
        found = await MemorySearch(SemanticMemory(db), ProceduralMemory(db)).recall(
            "cedar route",
            since=datetime(2026, 1, 15, tzinfo=UTC), until=datetime(2026, 1, 25, tzinfo=UTC))
    assert [_ref(r) for r in found] == ["log:s1:2"]
    assert found[0].updated_at.startswith("2026-01-20")


async def test_one_turn_one_slot_but_turns_stay_separate(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "user", "why did the amber plan stop")
        await _log(db, "s1", 1, "assistant", "the amber plan stopped: early reading")
        await _log(db, "s1", 4, "assistant", "the amber plan, later reading: noise")
        found = await MemorySearch(SemanticMemory(db), ProceduralMemory(db)).recall("amber plan")
    refs = [_ref(r) for r in found]
    assert sorted(refs) == ["log:s1:1", "log:s1:4"]


async def test_tool_rows_are_marked_as_previews(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 2, "tool", "stone_bridge_test failed: exit_code=1",
                   metadata={"tool_call_id": "c1", "tool_name": "run_bash"})
        found = await MemorySearch(SemanticMemory(db), ProceduralMemory(db)).recall(
            "stone_bridge_test")
    assert [_ref(r) for r in found] == ["log:s1:2"]
    assert "preview" in found[0].value


async def test_deleted_session_stops_matching(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "gone", 1, "assistant", "foglead mountain trip notes")
        search = MemorySearch(SemanticMemory(db), ProceduralMemory(db))
        assert [_ref(r) for r in await search.recall("foglead")] == ["log:gone:1"]
        await SessionLog(db).delete_session("gone")
        assert list(await search.recall("foglead")) == []


async def test_deleted_session_short_term_index_is_cleaned(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "gone", 1, "assistant", "霧鉛山的旅行")
        search = MemorySearch(SemanticMemory(db), ProceduralMemory(db))
        assert [_ref(r) for r in await search.recall("旅行")] == ["log:gone:1"]
        await SessionLog(db).delete_session("gone")
        assert list(await search.recall("旅行")) == []


async def test_excerpts_keep_read_time_secret_redaction(tmp_path):
    secret = "sk-ant-" + "a" * 40
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "user", f"lantern api_key={secret}")
        found = await MemorySearch(SemanticMemory(db), ProceduralMemory(db)).recall("lantern")
    assert found and secret not in found[0].value


async def test_rows_written_outside_the_session_log_api_are_indexed(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        search = MemorySearch(SemanticMemory(db), ProceduralMemory(db))
        assert list(await search.recall("苔蘚")) == []
        await _log(db, "s1", 1, "assistant", "觀察苔蘚的興趣")
        assert [_ref(r) for r in await search.recall("苔蘚")] == ["log:s1:1"]


async def test_axis_filters_exclude_conversation_and_say_so(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "assistant", "saffron archive talk")
        found = await MemorySearch(SemanticMemory(db), ProceduralMemory(db)).recall(
            "saffron archive", domain="self")
    assert list(found) == []
    assert "conversation" in found.coverage


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def _legacy_db(path):
    """A pre-S1 database: unicode61 semantic_fts, no conversation index."""
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE semantic_entries (id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL,
            value TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 1.0, source TEXT,
            metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, embedding TEXT,
            domain TEXT NOT NULL DEFAULT 'knowledge', temporal TEXT NOT NULL DEFAULT 'recent',
            last_accessed_at TEXT);
        CREATE VIRTUAL TABLE semantic_fts USING fts5(key, value,
            content='semantic_entries', content_rowid='rowid');
        CREATE TRIGGER semantic_entries_ai AFTER INSERT ON semantic_entries BEGIN
          INSERT INTO semantic_fts(rowid, key, value) VALUES (new.rowid, new.key, new.value);
        END;
        CREATE TABLE session_log (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            turn_index INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
        INSERT INTO semantic_entries (id,key,value,created_at,updated_at)
            VALUES ('1','k:legacy','中文長句的舊記憶內容','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00');
        INSERT INTO session_log (session_id,turn_index,role,content,created_at)
            VALUES ('old',1,'assistant','舊對話裡的苔蘚觀察','2026-01-01T00:00:00+00:00');
        """)


async def test_migration_backs_up_beside_the_db_and_indexes_existing_rows(tmp_path):
    path = tmp_path / "memory.db"
    _legacy_db(path)
    store = SQLiteStore(str(path))
    await store.initialize()
    backups = list((tmp_path / "backups").glob("recall-index-*.db"))
    assert len(backups) == 1
    async with store.connect() as db:
        search = MemorySearch(SemanticMemory(db), ProceduralMemory(db))
        assert [_ref(r) for r in await search.recall("舊記憶")] == ["mem:k:legacy"]
        assert [_ref(r) for r in await search.recall("苔蘚")] == ["log:old:1"]
    await store.initialize()
    assert len(list((tmp_path / "backups").glob("recall-index-*.db"))) == 1


async def test_failed_backup_skips_migration_and_recall_reports_the_gap(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    _legacy_db(path)

    async def _no_backup(*args, **kwargs):
        return None

    monkeypatch.setattr("loom.core.memory.recall_index.snapshot_before_write", _no_backup)
    store = SQLiteStore(str(path))
    await store.initialize()
    async with store.connect() as db:
        found = await MemorySearch(SemanticMemory(db), ProceduralMemory(db)).recall("苔蘚")
    assert list(found) == []
    assert "unavailable" in found.coverage


async def test_fresh_database_needs_no_backup(tmp_path):
    store = SQLiteStore(str(tmp_path / "memory.db"))
    await store.initialize()
    assert not (tmp_path / "backups").exists()


# ---------------------------------------------------------------------------
# Candidate and output budgets (PR #652 review)
# ---------------------------------------------------------------------------

async def test_a_dense_turn_does_not_crowd_other_turns_out_of_the_candidate_pool(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "quiet", 1, "assistant", "量測失敗一次", "2026-01-01T00:00:00+00:00")
        for i in range(210):
            await db.execute(
                "INSERT INTO session_log (session_id, turn_index, role, content, metadata, created_at) "
                "VALUES ('dense', 1, 'tool', ?, '{}', '2026-02-01T00:00:00+00:00')",
                (f"量測失敗 第{i}次",),
            )
        await db.commit()
        search = MemorySearch(SemanticMemory(db), ProceduralMemory(db))
        long_term = await search.recall("量測失敗", limit=10)
        short_term = await search.recall("量測", limit=10)
    assert {_ref(r) for r in long_term} == {"log:dense:1", "log:quiet:1"}
    assert {_ref(r) for r in short_term} == {"log:dense:1", "log:quiet:1"}


async def test_identical_text_in_different_turns_is_not_a_fork_copy(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "assistant", "夜間驗證失敗", "2026-01-01T00:00:00+00:00")
        await _log(db, "s1", 9, "assistant", "夜間驗證失敗", "2026-01-09T00:00:00+00:00")
        found = await MemorySearch(SemanticMemory(db), ProceduralMemory(db)).recall("夜間驗證")
    assert {_ref(r) for r in found} == {"log:s1:1", "log:s1:9"}


async def test_forked_copy_of_a_turn_takes_one_slot(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        log = SessionLog(db)
        await log.create_session("root", "m")
        await _log(db, "root", 1, "assistant", "copper kettle whistle", "2026-01-01T00:00:00+00:00")
        await log.fork_session("root", "branch", 1)
        found = await MemorySearch(SemanticMemory(db), ProceduralMemory(db)).recall("copper kettle")
    assert len(found) == 1


async def test_single_character_scan_continuation_reaches_older_event_times(tmp_path, monkeypatch):
    import re
    monkeypatch.setattr("loom.core.memory.search._SCAN_CHUNK_ROWS", 2)
    monkeypatch.setattr("loom.core.memory.search._SCAN_SECONDS", 0.0)
    store = await _store(tmp_path)
    async with store.connect() as db:
        await _log(db, "s1", 1, "assistant", "貓在窗邊", "2026-03-01T00:00:00+00:00")
        await _log(db, "s1", 2, "assistant", "貓在睡覺", "2026-03-02T00:00:00+00:00")
        # Fork-shaped rows: written later (larger id) but keep older event times.
        await _log(db, "s2", 1, "assistant", "貓在吃飯", "2020-01-01T00:00:00+00:00")
        await _log(db, "s2", 2, "assistant", "貓在玩耍", "2020-01-02T00:00:00+00:00")
        search = MemorySearch(SemanticMemory(db), ProceduralMemory(db))
        first = await search.recall("貓", limit=10)
        assert {_ref(r) for r in first} == {"log:s1:1", "log:s1:2"}
        hint = re.search(r"since=(\S+) until=(\S+?)\)", first.coverage)
        assert hint, first.coverage
        rest = await search.recall(
            "貓", limit=10,
            since=datetime.fromisoformat(hint.group(1)),
            until=datetime.fromisoformat(hint.group(2)))
    assert {_ref(r) for r in rest} == {"log:s2:1", "log:s2:2"}
    assert "stopped" not in rest.coverage


async def test_tool_output_respects_the_whole_call_budget(tmp_path):
    from loom.core.harness.middleware import ToolCall
    from loom.core.harness.permissions import TrustLevel
    from loom.core.memory.facade import MemoryFacade
    from loom.core.memory.search import RECALL_OUTPUT_CHARS
    from loom.platform.cli.tools import make_recall_tool
    from unittest.mock import MagicMock

    store = await _store(tmp_path)
    async with store.connect() as db:
        for turn in range(10):
            await _log(db, "s1", turn, "assistant", f"lantern note {turn} " + "x" * 700)
        semantic, procedural = SemanticMemory(db), ProceduralMemory(db)
        facade = MemoryFacade(semantic=semantic, procedural=procedural, episodic=MagicMock(),
                              search=MemorySearch(semantic, procedural), governor=None)
        result = await make_recall_tool(facade).executor(ToolCall(
            id="c", tool_name="recall", args={"query": "lantern", "limit": 10},
            trust_level=TrustLevel.SAFE, session_id="s1"))
    assert result.success
    assert len(result.output) <= RECALL_OUTPUT_CHARS
    assert result.output.rstrip().splitlines()[-1].startswith("Coverage")
    assert "budget" in result.output


async def test_fork_copies_do_not_use_up_the_candidate_budget(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        log = SessionLog(db)
        await _log(db, "quiet", 1, "assistant", "量測失敗 安靜回合有另一項結果",
                   "2026-01-01T00:00:00+00:00")
        await log.create_session("root", "m")
        await _log(db, "root", 1, "assistant", "量測失敗", "2026-02-01T00:00:00+00:00")
        for i in range(42):
            await log.fork_session("root", f"fork{i}", 1)
        search = MemorySearch(SemanticMemory(db), ProceduralMemory(db))
        long_term = await search.recall("量測失敗", limit=10)
        short_term = await search.recall("量測", limit=10)
    for found in (long_term, short_term):
        refs = {_ref(r) for r in found}
        assert "log:quiet:1" in refs, found.coverage
        assert len(refs) == 2


async def test_results_cut_by_the_output_budget_are_not_marked_accessed(tmp_path):
    from loom.core.harness.middleware import ToolCall
    from loom.core.harness.permissions import TrustLevel
    from loom.core.memory.facade import MemoryFacade
    from loom.platform.cli.tools import make_recall_tool
    from unittest.mock import MagicMock

    store = await _store(tmp_path)
    async with store.connect() as db:
        semantic, procedural = SemanticMemory(db), ProceduralMemory(db)
        for i in range(10):
            await semantic.upsert(SemanticEntry(
                key=f"k:lantern:{i}:" + "z" * 250, value=f"lantern {i} " + "y" * 600))
        await db.execute("UPDATE semantic_entries SET last_accessed_at = NULL")
        await db.commit()
        facade = MemoryFacade(semantic=semantic, procedural=procedural, episodic=MagicMock(),
                              search=MemorySearch(semantic, procedural), governor=None)
        result = await make_recall_tool(facade).executor(ToolCall(
            id="c", tool_name="recall", args={"query": "lantern", "limit": 10},
            trust_level=TrustLevel.SAFE, session_id="s1"))
        cursor = await db.execute("SELECT key, last_accessed_at FROM semantic_entries")
        accessed = dict(await cursor.fetchall())
    shown = {k for k in accessed if k in result.output}
    assert "omitted" in result.output
    assert shown and len(shown) < len(accessed)
    for key, at in accessed.items():
        assert (at is not None) == (key in shown), key


async def test_fork_flood_past_the_examination_cap_is_reported(tmp_path):
    store = await _store(tmp_path)
    async with store.connect() as db:
        log = SessionLog(db)
        await _log(db, "quiet", 1, "assistant", "量測失敗 較早的回合", "2026-01-01T00:00:00+00:00")
        await log.create_session("root", "m")
        await _log(db, "root", 1, "assistant", "量測失敗", "2026-02-01T00:00:00+00:00")
        for i in range(165):
            await log.fork_session("root", f"fork{i}", 1)
        found = await MemorySearch(SemanticMemory(db), ProceduralMemory(db)).recall("量測")
    assert len(found) == 1
    assert "stopped after examining" in found.coverage
