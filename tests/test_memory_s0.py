"""S0 evaluation validity and copy/DDL safety; no future runtime behavior.

Quality failures are baseline observations, not passing assertions about a new
retriever. All texts and databases in these tests are synthetic.
"""
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from scripts.memory_s0 import (
    CASES, build_fixture, checked_snapshot, create_log_probe, distribution,
    dump, evaluate, snapshot, summarize, synthetic,
)


def fixture_refs(fixture):
    refs = {"mem:" + r["key"]: r["value"] for r in fixture["semantic"]}
    refs.update({"skill:" + r["name"]: r["body"] for r in fixture["skills"]})
    for row in fixture["messages"]:
        key = f'log:{row["session_id"]}:{row["turn_index"]}'
        refs[key] = refs.get(key, "") + "\n" + row["content"]
    refs.update({r["ref"]: r["content"] for r in fixture["documents"]})
    return refs


def test_gold_is_resolvable_and_holdout_covers_both_outcomes():
    fixture = json.loads(CASES.read_text())
    cases = fixture["cases"]
    assert len({c["id"] for c in cases}) == len(cases)
    assert sum(bool(c["expected"]) for c in cases) >= 24
    assert sum(not c["expected"] for c in cases) >= 8
    assert sum(c["stage"] == "S1" and bool(c["expected"]) for c in cases) >= 24
    assert any(c["split"] == "holdout" and c["expected"] for c in cases)
    assert any(c["split"] == "holdout" and not c["expected"] for c in cases)
    refs = fixture_refs(fixture)
    for case in cases:
        for gold in case["expected"]:
            assert gold["ref"] in refs
            assert gold["contains"]
            assert all(part in refs[gold["ref"]] for part in gold["contains"])


def test_same_session_turns_remain_distinct_gold():
    fixture = json.loads(CASES.read_text())
    case = next(c for c in fixture["cases"] if c["id"] == "log-before-after-B")
    assert [g["ref"] for g in case["expected"]] == [
        "log:s0-interpretation:1", "log:s0-interpretation:4",
    ]
    one = [{"ref": case["expected"][0]["ref"], "excerpt": "故意回避，沒有驗證"}]
    assert evaluate(case, one)["hit_at_5"]
    assert not evaluate(case, one)["all_sources_located"]


def test_topic_match_or_wrong_excerpt_cannot_pass_source_metric():
    case = {"expected": [{"ref": "log:s0:1", "contains": ["原始證據"]}]}
    assert not evaluate(case, [{"ref": "mem:similar", "excerpt": "原始證據"}])["hit_at_5"]
    assert not evaluate(case, [{"ref": "log:s0:1", "excerpt": "別的內容"}])["hit_at_5"]
    assert not evaluate(case, ([{"ref": "mem:noise", "excerpt": ""}] * 5
                              + [{"ref": "log:s0:1", "excerpt": "原始證據"}]))["hit_at_5"]


def test_recency_fallback_is_not_an_answer_to_a_negative():
    result = evaluate({"expected": []}, [{"ref": "mem:recent", "excerpt": "最近的事"}])
    assert result["false_positive"]
    assert not result["empty"]
    summary = summarize([{"evaluation": result}])
    assert summary["false_positive_rate"] == 1


def test_percentile_is_documented_nearest_rank():
    assert distribution(list(range(1, 32)))["p95"] == 30
    assert distribution([]) == {"n": 0}


def test_snapshot_source_is_unchanged_and_destination_is_exclusive(tmp_path):
    source, destination = tmp_path / "source.db", tmp_path / "snapshot.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE sample(value)")
        db.execute("INSERT INTO sample VALUES ('synthetic source')")
    before = source.read_bytes()
    snapshot(source, destination)
    assert checked_snapshot(destination) == destination.resolve()
    assert source.read_bytes() == before
    with sqlite3.connect(destination) as db:
        db.execute("DELETE FROM sample")
    with sqlite3.connect(source) as db:
        assert db.execute("SELECT value FROM sample").fetchone() == ("synthetic source",)
    with pytest.raises(ValueError):
        snapshot(source, destination)
    with pytest.raises(ValueError):
        snapshot(source, source)


def test_unmarked_or_live_benchmark_source_is_rejected(tmp_path):
    source = tmp_path / "arbitrary.db"
    source.write_bytes(b"not a snapshot")
    with pytest.raises(ValueError):
        checked_snapshot(source)
    with pytest.raises(ValueError):
        checked_snapshot(Path.home() / ".loom" / "memory.db")


def test_json_output_cannot_overwrite_a_database_or_follow_a_live_alias(tmp_path):
    database = tmp_path / "source.db"
    database.write_bytes(b"synthetic database")
    with pytest.raises(ValueError):
        dump(database, {})
    assert database.read_bytes() == b"synthetic database"
    alias = tmp_path / "report.json"
    alias.symlink_to(Path.home() / ".loom" / "report.json")
    with pytest.raises(ValueError):
        dump(alias, {})


def test_truncated_external_content_has_consistent_rebuild_update_delete():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE session_log(id INTEGER PRIMARY KEY,role,content)")
    create_log_probe(db, 12)
    db.execute("INSERT INTO session_log VALUES (1,'tool','prefixabcdef hiddenneedle')")
    db.execute("INSERT INTO session_log VALUES (2,'assistant','visible assistant needle')")
    assert db.execute("SELECT COUNT(*) FROM s0_log_fts WHERE s0_log_fts MATCH 'hiddenneedle'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM s0_log_fts WHERE s0_log_fts MATCH 'assistant'").fetchone()[0] == 1
    db.execute("INSERT INTO s0_log_fts(s0_log_fts) VALUES ('rebuild')")
    assert db.execute("SELECT COUNT(*) FROM s0_log_fts WHERE s0_log_fts MATCH 'hiddenneedle'").fetchone()[0] == 0
    db.execute("UPDATE session_log SET content='changedprefix hiddenneedle' WHERE id=1")
    db.execute("UPDATE session_log SET role='system' WHERE id=2")
    db.execute("DELETE FROM session_log WHERE id=1")
    db.execute("INSERT INTO s0_log_fts(s0_log_fts,rank) VALUES ('integrity-check',1)")
    assert db.execute("SELECT COUNT(*) FROM s0_log_fts WHERE s0_log_fts MATCH 'assistant'").fetchone()[0] == 0
    db.close()


def test_trigram_two_character_limit_is_visible():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE sample USING fts5(content,tokenize='trigram')")
    db.execute("INSERT INTO sample VALUES ('先確認再放下清單')")
    assert db.execute("SELECT COUNT(*) FROM sample WHERE sample MATCH '放下'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM sample WHERE sample MATCH '放下清單'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM sample WHERE content LIKE '%放下%'").fetchone()[0] == 1
    db.close()


async def test_fixture_is_built_only_at_explicit_path(tmp_path):
    fixture = json.loads(CASES.read_text())
    store = await build_fixture(tmp_path / "fixture.db", fixture)
    async with store.connect() as db:
        row = await (await db.execute("SELECT COUNT(*) FROM session_log")).fetchone()
        assert row[0] == len(fixture["messages"])


async def test_frozen_embeddings_are_only_synthetic_and_replayable(tmp_path):
    path = CASES.with_name("memory_s0_embeddings.json.gz")
    with gzip.open(path, "rt") as stream:
        cache = json.load(stream)
    assert cache["cases_sha256"] == hashlib.sha256(CASES.read_bytes()).hexdigest()
    result = await synthetic(tmp_path / "replay.json", None, "unused", path)
    assert len(result["cases"]) == len(json.loads(CASES.read_text())["cases"])
    assert result["profile"] == "ollama_replay"
    # Frozen baseline remains the pre-S1 observation. Do not assert that a
    # future runtime must keep its poor hit/false-positive rates.
    frozen = json.loads(CASES.with_name("memory_s0_baseline.json").read_text())
    assert frozen["cases_sha256"] == cache["cases_sha256"]
    assert len(frozen["cases"]) == len(result["cases"])
