"""#650 S2 contract tests — document collections in recall (spec 61 r3 §9.2).

Written before the implementation (red → green).  Every file and database
here is synthetic and lives under ``tmp_path``.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio

from loom.core.memory import documents as docs_mod
from loom.core.memory.documents import (
    DocumentIndex,
    format_doc_ref,
    parse_doc_ref,
    split_sections,
)
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.search import MemorySearch
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.store import SQLiteStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ws(tmp_path) -> Path:
    root = tmp_path / "ws"
    (root / "notes").mkdir(parents=True)
    (root / "wiki").mkdir()
    return root


@pytest_asyncio.fixture
async def db(tmp_path):
    store = SQLiteStore(str(tmp_path / "memory.db"))
    await store.initialize()
    async with store.connect() as conn:
        yield conn


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _index(db, ws, collections=None) -> DocumentIndex:
    if collections is None:
        collections = {"notes": "notes", "wiki": "wiki"}
    return DocumentIndex(db, ws, collections)


def _search(db, index) -> MemorySearch:
    return MemorySearch(SemanticMemory(db), ProceduralMemory(db), documents=index)


async def _docs(search: MemorySearch, query: str, **kw):
    results = await search.recall(query, **kw)
    return [r for r in results if r.type == "document"], results.coverage


RIVER = (
    "# 風中的玻璃\n"
    "河面倒影破碎，並不是河流受傷；這段沒有另寫成fact。\n\n"
    "## 後記\n"
    "我不確定自己是否把安慰投射到河流上，這個保留意見應與正文一起保留。\n"
)


# ---------------------------------------------------------------------------
# Sections and refs
# ---------------------------------------------------------------------------

class TestSections:
    def test_heading_path_preamble_and_body_levels(self):
        text = (
            "前言一句\n"
            "# 書\n本文\n"
            "## 第一篇\n內容A\n#### 小節仍屬第一篇\n內容B\n"
            "### 細節\n內容C\n"
        )
        secs = split_sections(text)
        anchors = [s.anchor for s in secs]
        assert anchors == ["_preamble", "書", "書/第一篇", "書/第一篇/細節"]
        first = secs[2]
        assert "小節仍屬第一篇" in first.content and "內容B" in first.content
        # the section text includes its own heading line
        assert first.content.startswith("## 第一篇")

    def test_fenced_hash_is_not_a_heading(self):
        text = "# 正文\n```bash\n# not a heading\necho hi\n```\n後文\n"
        secs = split_sections(text)
        assert [s.anchor for s in secs] == ["正文"]
        assert "# not a heading" in secs[0].content

    def test_duplicate_headings_are_numbered(self):
        text = "# 日記\n## 心得\nA\n## 心得\nB\n"
        anchors = [s.anchor for s in split_sections(text)]
        assert anchors == ["日記", "日記/心得", "日記/心得~2"]

    def test_empty_document_has_no_sections(self):
        assert split_sections("") == []
        assert split_sections("\n\n  \n") == []

    @pytest.mark.parametrize("path,anchor", [
        ("a b/c#d@e%f.md", "標題 #1/@x%"),
        ("讀書/閱讀筆記.md", "《給冥王星》張惠菁/2026-10-24 讀〈姨丈〉"),
    ])
    def test_ref_round_trip(self, path, anchor):
        ref = format_doc_ref("notes", path, anchor, "0123456789abcdef")
        assert ref.endswith("@01234567")
        parsed = parse_doc_ref(ref)
        assert parsed == ("notes", path, anchor, "01234567")

    def test_ref_without_version_parses(self):
        assert parse_doc_ref("doc:notes/x.md#a") == ("notes", "x.md", "a", None)
        assert parse_doc_ref("doc:notes") is None


# ---------------------------------------------------------------------------
# Finding sections
# ---------------------------------------------------------------------------

class TestRecallDocuments:
    async def test_section_found_without_any_fact(self, db, ws):
        _write(ws / "notes" / "river.md", RIVER)
        search = _search(db, _index(db, ws))
        found, coverage = await _docs(search, "倒影破碎")
        assert len(found) == 1
        assert found[0].ref.startswith("doc:notes/river.md#風中的玻璃@")
        assert "沒有另寫成fact" in found[0].value
        assert "documents" in coverage

    async def test_two_char_cjk_and_identifier_terms(self, db, ws):
        _write(ws / "wiki" / "p.md", "# 方案\n驗證 asset-set digest 作為信任錨點\n")
        search = _search(db, _index(db, ws))
        assert (await _docs(search, "錨點"))[0]
        assert (await _docs(search, "asset-set"))[0]
        assert (await _docs(search, "信 錨點"))[0]   # single CJK char + bigram

    async def test_documents_never_short_circuit_memory(self, db, ws):
        _write(ws / "notes" / "n.md", "# 茶\n烏龍茶的焙火筆記\n")
        sem = SemanticMemory(db)
        await sem.upsert(SemanticEntry(key="tea", value="烏龍茶 偏好"))
        search = MemorySearch(sem, ProceduralMemory(db), documents=_index(db, ws))
        results = await search.recall("烏龍茶")
        types = {r.type for r in results}
        assert {"semantic", "document"} <= types

    async def test_no_match_is_empty(self, db, ws):
        _write(ws / "notes" / "river.md", RIVER)
        search = _search(db, _index(db, ws))
        results = await search.recall("香蕉烘焙溫度")
        assert list(results) == []

    async def test_type_and_time_filters_exclude_documents(self, db, ws):
        _write(ws / "notes" / "river.md", RIVER)
        search = _search(db, _index(db, ws))
        found, cov = await _docs(search, "倒影破碎", type="semantic")
        assert found == [] and "documents: excluded" in cov
        found, cov = await _docs(
            search, "倒影破碎", since=datetime(2020, 1, 1, tzinfo=UTC))
        assert found == [] and "documents: excluded" in cov

    async def test_unconfigured_collections_report_gap(self, db, ws):
        search = _search(db, _index(db, ws, collections={}))
        _, cov = await _docs(search, "anything")
        assert "documents: not configured" in cov

    async def test_only_configured_collections_and_markdown(self, db, ws):
        _write(ws / "other" / "x.md", "# 外部\n倒影破碎\n")
        _write(ws / "notes" / "x.txt", "倒影破碎")
        _write(ws / "notes" / ".hidden" / "x.md", "# 隱藏\n倒影破碎\n")
        search = _search(db, _index(db, ws))
        assert (await _docs(search, "倒影破碎"))[0] == []

    async def test_replay_is_idempotent(self, db, ws):
        _write(ws / "notes" / "river.md", RIVER)
        index = _index(db, ws)
        await index.sync()
        await index.sync()
        cur = await db.execute("SELECT COUNT(*) FROM doc_versions")
        assert (await cur.fetchone())[0] == 2


# ---------------------------------------------------------------------------
# Revisions (A1)
# ---------------------------------------------------------------------------

class TestRevisions:
    async def test_new_heading_keeps_existing_refs(self, db, ws):
        f = _write(ws / "notes" / "book.md", "# 書\n## 第一篇\n光頭報告\n")
        search = _search(db, _index(db, ws))
        before = (await _docs(search, "光頭報告"))[0][0].ref
        _write(f, f.read_text() + "## 第二篇\n為了追見一節竹子\n")
        after = (await _docs(search, "光頭報告"))[0]
        assert [r.ref for r in after] == [before]
        assert "current" in after[0].format()

    async def test_edit_creates_version_old_searchable_and_marked(self, db, ws):
        f = _write(ws / "wiki" / "proposal.md",
                   "# 選項\n舊提案只驗證單一說明檔，尚未採用。\n")
        search = _search(db, _index(db, ws))
        old_ref = (await _docs(search, "單一說明檔"))[0][0].ref
        _write(f, "# 選項\n新版移除單檔驗證，改成資產集合。\n")
        found = (await _docs(search, "單一說明檔"))[0]
        assert [r.ref for r in found] == [old_ref]
        text = found[0].format()
        assert "old version" in text
        cur_ref = (await _docs(search, "資產集合"))[0][0].ref
        assert cur_ref != old_ref and cur_ref in text
        # identity read of the old ref returns the old text, marked
        exp = await search.expand(old_ref)
        assert exp.found and "舊提案只驗證單一說明檔" in exp.text
        assert "old version" in exp.text and cur_ref in exp.text
        assert "not saved" in exp.text   # neighbours of that time were not kept

    async def test_append_under_last_heading_changes_ref(self, db, ws):
        f = _write(ws / "notes" / "d.md", "# 讀後感\n第一段\n")
        search = _search(db, _index(db, ws))
        r1 = (await _docs(search, "第一段"))[0][0].ref
        _write(f, "# 讀後感\n第一段\n第二段\n")
        refs = [r.ref for r in (await _docs(search, "第一段"))[0]]
        assert r1 in refs and len(refs) == 2   # old + current, both shown

    async def test_current_is_unique_after_abc_and_aba(self, db, ws):
        f = _write(ws / "notes" / "v.md", "# 段\n版本甲內容\n")
        index = _index(db, ws)
        search = _search(db, index)
        ref_a = (await _docs(search, "版本甲"))[0][0].ref
        _write(f, "# 段\n版本乙內容\n")
        await index.sync()
        _write(f, "# 段\n版本丙內容\n")
        ref_c = (await _docs(search, "版本丙"))[0][0].ref
        exp = await search.expand(ref_a)
        assert ref_c in exp.text            # the current version, not the next one
        _write(f, "# 段\n版本甲內容\n")       # A→B→C→A
        found = (await _docs(search, "版本甲"))[0]
        assert [r.ref for r in found] == [ref_a]
        assert "current" in found[0].format()
        cur = await db.execute("SELECT COUNT(*) FROM doc_current")
        assert (await cur.fetchone())[0] == 1

    async def test_section_gone_from_file(self, db, ws):
        f = _write(ws / "notes" / "g.md", "# 甲\n留下的段落\n# 乙\n會被拿掉的段落\n")
        search = _search(db, _index(db, ws))
        ref = (await _docs(search, "會被拿掉"))[0][0].ref
        _write(f, "# 甲\n留下的段落\n")
        found = (await _docs(search, "會被拿掉"))[0]
        assert found and "no longer in the file" in found[0].format()
        exp = await search.expand(ref)
        assert exp.found and "no longer in the file" in exp.text

    async def test_section_versions_take_at_most_two_slots(self, db, ws):
        f = _write(ws / "notes" / "m.md", "# 段\n共同詞 v0\n")
        index = _index(db, ws)
        search = _search(db, index)
        for i in range(1, 6):
            _write(f, f"# 段\n共同詞 v{i}\n")
            await index.sync()
        found = (await _docs(search, "共同詞", limit=10))[0]
        assert len(found) <= 2
        assert any("current" in r.format() for r in found)

    async def test_unsaved_version_shows_current_with_note(self, db, ws):
        _write(ws / "notes" / "u.md", "# 段\n現在的內容\n")
        search = _search(db, _index(db, ws))
        exp = await search.expand("doc:notes/u.md#段@deadbeef")
        assert exp.found and "現在的內容" in exp.text
        assert "@deadbeef" in exp.text and "not saved" in exp.text


# ---------------------------------------------------------------------------
# Deletion, scope and moves
# ---------------------------------------------------------------------------

class TestDeleteScopeMove:
    async def test_deleted_file_only_reports_status(self, db, ws):
        f = _write(ws / "notes" / "river.md", RIVER)
        search = _search(db, _index(db, ws))
        ref = (await _docs(search, "倒影破碎"))[0][0].ref
        f.unlink()
        assert (await _docs(search, "倒影破碎"))[0] == []
        exp = await search.expand(ref)
        assert not exp.found and "deleted or moved out" in exp.text
        assert "倒影破碎" not in exp.text

    async def test_restored_file_reconnects_history(self, db, ws):
        f = _write(ws / "notes" / "river.md", RIVER)
        search = _search(db, _index(db, ws))
        ref = (await _docs(search, "倒影破碎"))[0][0].ref
        f.unlink()
        await _docs(search, "倒影破碎")
        _write(f, RIVER)
        found = (await _docs(search, "倒影破碎"))[0]
        assert [r.ref for r in found] == [ref]

    async def test_removed_collection_and_changed_root_hide_content(self, db, ws):
        _write(ws / "notes" / "river.md", RIVER)
        search = _search(db, _index(db, ws))
        ref = (await _docs(search, "倒影破碎"))[0][0].ref
        # collection withdrawn from config
        s2 = _search(db, _index(db, ws, collections={"wiki": "wiki"}))
        assert (await _docs(s2, "倒影破碎"))[0] == []
        exp = await s2.expand(ref)
        assert not exp.found and "倒影破碎" not in exp.text
        # same name, different root
        _write(ws / "elsewhere" / "z.md", "# z\nzzz\n")
        s3 = _search(db, _index(db, ws, collections={"notes": "elsewhere"}))
        assert (await _docs(s3, "倒影破碎"))[0] == []
        exp = await s3.expand(ref)
        assert not exp.found and "倒影破碎" not in exp.text

    async def test_unique_whole_file_move_keeps_history(self, db, ws):
        f = _write(ws / "wiki" / "draft.md", "# 段\n第一版想法\n")
        index = _index(db, ws)
        search = _search(db, index)
        old_v1 = (await _docs(search, "第一版想法"))[0][0].ref
        _write(f, "# 段\n第二版想法\n")
        old_v2 = (await _docs(search, "第二版想法"))[0][0].ref
        dest = ws / "notes" / "archive" / "draft.md"   # across collections
        dest.parent.mkdir(parents=True)
        f.rename(dest)
        found = (await _docs(search, "第一版想法"))[0]
        assert found and found[0].ref.startswith("doc:notes/archive/draft.md#")
        assert "old version" in found[0].format()
        exp = await search.expand(old_v2)
        assert exp.found and "moved to" in exp.text and "doc:notes/archive/draft.md" in exp.text
        assert "第二版想法" in exp.text
        assert old_v1  # history carried, still resolvable through the move

    async def test_move_with_edit_is_delete_plus_new(self, db, ws):
        f = _write(ws / "wiki" / "a.md", "# 段\n原始內容\n")
        search = _search(db, _index(db, ws))
        ref = (await _docs(search, "原始內容"))[0][0].ref
        f.unlink()
        _write(ws / "wiki" / "b.md", "# 段\n原始內容 加一句\n")
        exp = await search.expand(ref)
        assert not exp.found and "deleted or moved out" in exp.text
        found = (await _docs(search, "原始內容"))[0]
        assert [r.ref.split("#")[0] for r in found] == ["doc:wiki/b.md"]
        assert "old version" not in found[0].format()

    async def test_ambiguous_move_is_not_a_move(self, db, ws):
        f = _write(ws / "wiki" / "a.md", "# 段\n同樣的內容\n")
        search = _search(db, _index(db, ws))
        ref = (await _docs(search, "同樣的內容"))[0][0].ref
        f.unlink()
        _write(ws / "wiki" / "b.md", "# 段\n同樣的內容\n")
        _write(ws / "wiki" / "c.md", "# 段\n同樣的內容\n")
        exp = await search.expand(ref)
        assert not exp.found and "moved to" not in exp.text

    async def test_symlinks_are_refused(self, db, ws, tmp_path):
        outside = _write(tmp_path / "secret" / "s.md", "# 外\n外部祕密段落\n")
        (ws / "notes" / "link.md").symlink_to(outside)
        (ws / "notes" / "linkdir").symlink_to(outside.parent, target_is_directory=True)
        search = _search(db, _index(db, ws))
        assert (await _docs(search, "外部祕密"))[0] == []
        exp = await search.expand("doc:notes/link.md#外")
        assert not exp.found and "外部祕密" not in exp.text

    async def test_ref_traversal_is_refused(self, db, ws, tmp_path):
        _write(tmp_path / "ws" / "x.md", "# 外\n範圍外內容\n")
        search = _search(db, _index(db, ws))
        exp = await search.expand("doc:notes/../x.md#外")
        assert not exp.found and "範圍外內容" not in exp.text

    async def test_unreadable_file_is_not_deleted(self, db, ws):
        f = _write(ws / "notes" / "p.md", "# 段\n權限測試內容\n")
        search = _search(db, _index(db, ws))
        ref = (await _docs(search, "權限測試"))[0][0].ref
        _write(f, "# 段\n權限測試內容 改過\n")
        os.chmod(f, 0)
        try:
            _, cov = await _docs(search, "權限測試")
            assert "unreadable" in cov
            cur = await db.execute("SELECT state FROM doc_files WHERE path='p.md'")
            assert (await cur.fetchone())[0] == "present"
        finally:
            os.chmod(f, 0o644)
        assert ref


# ---------------------------------------------------------------------------
# Change detection and bounds
# ---------------------------------------------------------------------------

class TestDetection:
    async def test_same_size_edit_with_restored_mtime_is_seen(self, db, ws):
        f = _write(ws / "notes" / "t.md", "# 段\n內容甲甲\n")
        search = _search(db, _index(db, ws))
        assert (await _docs(search, "內容甲甲"))[0]
        st = f.stat()
        time.sleep(0.01)
        _write(f, "# 段\n內容乙乙\n")
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
        assert f.stat().st_size == st.st_size
        found = (await _docs(search, "內容乙乙"))[0]
        assert found and "current" in found[0].format()

    async def test_bounded_sync_reports_pending_and_catches_up(self, db, ws):
        for i in range(5):
            _write(ws / "notes" / f"n{i}.md", f"# 段\n批次內容{i}\n")
        index = DocumentIndex(db, ws, {"notes": "notes"}, max_files=2)
        search = _search(db, index)
        _, cov = await _docs(search, "批次內容")
        assert "files pending" in cov
        for _ in range(3):
            await index.sync()
        found, cov = await _docs(search, "批次內容", limit=10)
        assert len(found) == 5 and "pending" not in cov

    async def test_busy_file_does_not_starve_others(self, db, ws):
        busy = _write(ws / "notes" / "a_busy.md", "# 段\n忙碌 0\n")
        for i in range(3):
            _write(ws / "notes" / f"z{i}.md", f"# 段\n安靜內容{i}\n")
        index = DocumentIndex(db, ws, {"notes": "notes"}, max_files=1)
        for n in range(6):
            _write(busy, f"# 段\n忙碌 {n + 1}\n")
            await index.sync()
        cur = await db.execute("SELECT COUNT(*) FROM doc_files WHERE path LIKE 'z%'")
        assert (await cur.fetchone())[0] == 3

    async def test_change_during_read_is_deferred(self, db, ws, monkeypatch):
        f = _write(ws / "notes" / "r.md", "# 段\n讀取中內容\n")
        index = _index(db, ws)
        real = docs_mod._read_bytes

        def changing(fd, limit):
            data = real(fd, limit)
            _write(f, "# 段\n讀取中內容 已改成更長的版本\n")
            return data

        monkeypatch.setattr(docs_mod, "_read_bytes", changing)
        report = await index.sync()
        assert report.pending >= 1
        cur = await db.execute("SELECT COUNT(*) FROM doc_versions")
        assert (await cur.fetchone())[0] == 0
        monkeypatch.setattr(docs_mod, "_read_bytes", real)
        await index.sync()
        search = _search(db, index)
        found = (await _docs(search, "更長的版本"))[0]
        assert found

    async def test_oversized_file_reported(self, db, ws):
        _write(ws / "notes" / "big.md", "# 段\n" + "很長" * 50)
        index = DocumentIndex(db, ws, {"notes": "notes"}, max_file_bytes=64)
        search = _search(db, index)
        _, cov = await _docs(search, "很長")
        assert "too large" in cov


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------

class TestExpansion:
    async def test_current_expansion_has_neighbours(self, db, ws):
        _write(ws / "notes" / "river.md", "# 前篇\n前篇文字\n" + RIVER)
        search = _search(db, _index(db, ws))
        ref = (await _docs(search, "倒影破碎"))[0][0].ref
        exp = await search.expand(ref)
        assert exp.found
        assert "倒影破碎" in exp.text and "前篇文字" in exp.text and "保留意見" in exp.text
        assert "current" in exp.text

    async def test_long_section_is_cut_with_marker(self, db, ws):
        _write(ws / "notes" / "long.md", "# 長篇\n開頭標記\n" + "字" * 20_000)
        search = _search(db, _index(db, ws))
        ref = (await _docs(search, "開頭標記"))[0][0].ref
        exp = await search.expand(ref)
        assert exp.found and "cut:" in exp.text and len(exp.text) <= 8_000

    async def test_secrets_redacted_in_results_and_expansion(self, db, ws):
        _write(ws / "notes" / "s.md",
               '# 設定\n洩漏測試 api_key="abcdefgh12345678"\n')
        search = _search(db, _index(db, ws))
        found = (await _docs(search, "洩漏測試"))[0]
        assert "abcdefgh12345678" not in found[0].value
        exp = await search.expand(found[0].ref)
        assert "abcdefgh12345678" not in exp.text

    async def test_status_survives_recall_tool_budget(self, db, ws):
        from loom.core.harness.middleware import ToolCall
        from loom.core.harness.permissions import TrustLevel
        from loom.core.memory.facade import MemoryFacade
        from loom.platform.cli.tools import make_recall_tool
        from unittest.mock import MagicMock

        f = _write(ws / "notes" / "o.md", "# 段\n舊說法 " + "長" * 3000 + "\n")
        index = _index(db, ws)
        search = _search(db, index)
        await _docs(search, "舊說法")
        _write(f, "# 段\n新說法\n")
        sem = SemanticMemory(db)
        facade = MemoryFacade(semantic=sem, procedural=ProceduralMemory(db),
                              episodic=MagicMock(), search=search, governor=None)
        tool = make_recall_tool(facade)
        call = ToolCall(id="c", tool_name="recall", args={"query": "舊說法"},
                        trust_level=TrustLevel.SAFE, session_id="s")
        out = await tool.executor(call)
        assert out.success and "old version" in out.output


async def test_changed_root_never_lends_old_history_to_same_path(db, ws):
    _write(ws / "notes" / "same.md", "# 段\n舊根的祕密版本\n")
    search = _search(db, _index(db, ws))
    assert (await _docs(search, "舊根的祕密"))[0]
    _write(ws / "elsewhere" / "same.md", "# 段\n新根的內容\n")
    s2 = _search(db, _index(db, ws, collections={"notes": "elsewhere"}))
    assert (await _docs(s2, "新根的內容"))[0]
    assert (await _docs(s2, "舊根的祕密"))[0] == []
