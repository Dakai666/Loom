"""The opt-in diagnostic cleanup operates only on temporary databases."""

import json
import sqlite3

import pytest
from click.testing import CliRunner

from loom.core.memory.store import SQLiteStore
from loom.platform.cli.main import cli


async def _seed(tmp_path):
    db = tmp_path / "memory.db"
    await SQLiteStore(str(db)).initialize()
    with sqlite3.connect(db) as conn:
        for ident, key, source in [
            ("d1", "skill:deep:diagnostic:2026-11-19", "task_reflector:s1"),
            ("d2", "skill:light:diagnostic:2026-11-18", "task_reflector:s2"),
            ("keep-source", "skill:deep:diagnostic:manual", "manual"),
            ("keep-key", "skill:deep:other", "task_reflector:s1"),
            ("keep-skill", "skill:deep:evolution_hint:old", "skill_evolution"),
        ]:
            conn.execute(
                "INSERT INTO semantic_entries(id,key,value,source,metadata,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)", (ident, key, "unique evidence " + ident,
                                          source, '{"audit":true}', "2026-11-19", "2026-11-19"),
            )
    return db


def _run(db, *args):
    result = CliRunner().invoke(cli, ["memory", "retire-skill-diagnostics", "--db", str(db), *args])
    assert result.exit_code == 0, result.output + repr(result.exception)
    return json.loads(result.output)


async def test_dry_run_manifest_execute_backup_export_and_rerun(tmp_path):
    db = await _seed(tmp_path)
    artifacts = tmp_path / "artifacts"
    manifest = _run(db)
    assert {r["id"] for r in manifest["manifest"]} == {"d1", "d2"}
    assert all(set(r) == {"id", "key"} for r in manifest["manifest"])
    assert manifest["deleted"] == 0
    assert manifest["backup"] is None and manifest["export"] is None
    assert not artifacts.exists()
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT * FROM semantic_entries ORDER BY id").fetchall()
    done = _run(db, "--execute", "--output-dir", str(artifacts))
    assert done["deleted"] == 2
    assert done["integrity_check"] == "ok" and done["fts_check"] == "ok"
    with sqlite3.connect(done["backup"]) as conn:
        assert conn.execute("SELECT * FROM semantic_entries ORDER BY id").fetchall() == before
    with open(done["export"]) as f:
        exported = json.load(f)
    assert {r["id"] for r in exported} == {"d1", "d2"}
    assert all(r["value"] and r["metadata"] == '{"audit":true}' for r in exported)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM semantic_entries ORDER BY id").fetchall() == [
            r for r in before if r[0] not in {"d1", "d2"}
        ]
        assert conn.execute(
            "SELECT key FROM semantic_fts WHERE semantic_fts MATCH ?",
            ('"unique evidence d1"',),
        ).fetchall() == []
    again = _run(db, "--execute", "--output-dir", str(artifacts))
    assert again["manifest"] == [] and again["deleted"] == 0
    assert again["backup"] is None and again["export"] is None


async def test_fts_corruption_rolls_back_delete_and_keeps_backup(tmp_path):
    db = await _seed(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO semantic_fts(semantic_fts) VALUES('delete-all')")
    result = CliRunner().invoke(cli, ["memory", "retire-skill-diagnostics", "--db", str(db),
                                    "--execute", "--output-dir", str(tmp_path / "artifacts")])
    assert result.exit_code != 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM semantic_entries").fetchone()[0] == 5
    assert list((tmp_path / "artifacts").rglob("*.db"))


def test_missing_db_dry_run_does_not_create_it(tmp_path):
    db = tmp_path / "missing.db"
    result = CliRunner().invoke(cli, ["memory", "retire-skill-diagnostics", "--db", str(db)])
    assert result.exit_code != 0
    assert not db.exists()


async def test_online_backup_includes_committed_wal_and_holds_writer_lock(tmp_path, monkeypatch):
    from loom.core.memory import retire_diagnostics

    db = await _seed(tmp_path)
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE semantic_entries SET value='committed WAL evidence' WHERE id='d1'")
        writer.commit()
        # A competing writer must be excluded before artifacts are created.
        original_mkdtemp = retire_diagnostics.tempfile.mkdtemp

        def check_writer_lock(*args, **kwargs):
            with sqlite3.connect(db, timeout=0) as contender:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    contender.execute("BEGIN IMMEDIATE")
            return original_mkdtemp(*args, **kwargs)

        monkeypatch.setattr(retire_diagnostics.tempfile, "mkdtemp", check_writer_lock)
        done = _run(db, "--execute")
        with sqlite3.connect(done["backup"]) as backup:
            assert backup.execute("SELECT value FROM semantic_entries WHERE id='d1'").fetchone()[0] == "committed WAL evidence"
    finally:
        writer.close()


@pytest.mark.parametrize("stage", ["backup", "export"])
async def test_artifact_failure_does_not_delete_rows(tmp_path, monkeypatch, stage):
    from loom.core.memory import retire_diagnostics

    db = await _seed(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("artifact disk failure")

    if stage == "backup":
        # Failure before online backup begins.
        monkeypatch.setattr(retire_diagnostics.os, "chmod", fail)
    else:
        monkeypatch.setattr(retire_diagnostics.json, "dump", fail)
    result = CliRunner().invoke(cli, ["memory", "retire-skill-diagnostics", "--db", str(db),
                                    "--execute", "--output-dir", str(tmp_path / "artifacts")])
    assert result.exit_code != 0 and "artifact disk failure" in result.output
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM semantic_entries").fetchone()[0] == 5
