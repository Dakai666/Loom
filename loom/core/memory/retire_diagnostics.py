"""Opt-in retirement of exact TaskReflector semantic rows (#675).

No startup migration calls this module. Dry runs open an existing DB read-only;
execution holds a write reservation while selecting, backing up, and deleting.
"""

from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile


def retire_skill_diagnostics(
    db_path: Path, *, execute: bool = False, output_dir: Path | None = None,
) -> dict:
    """Return an exact manifest; optionally back up/export/delete atomically.

    Integrity checks happen before commit so a failed check rolls back every
    deletion. Backup and export survive failures for recovery and inspection.
    """
    path = db_path.expanduser().resolve(strict=True)
    mode = "rw" if execute else "ro"
    with closing(sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=5)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE" if execute else "BEGIN")
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM semantic_entries "
            "WHERE key GLOB 'skill:*:diagnostic:*' AND source GLOB 'task_reflector:*' "
            "ORDER BY id"
        )]
        result = {
            "manifest": [{"id": r["id"], "key": r["key"]} for r in rows],
            "deleted": 0, "backup": None, "export": None,
            "integrity_check": None, "fts_check": None,
        }
        if not execute or not rows:
            return result

        parent = output_dir.expanduser().resolve() if output_dir else path.parent
        parent.mkdir(parents=True, exist_ok=True)
        artifacts = Path(tempfile.mkdtemp(prefix="skill-diagnostic-retirement-", dir=parent))
        backup = artifacts / "memory.db"
        export = artifacts / "selected-rows.json"
        # Backing up the connection holding a write transaction waits on itself.
        # A separate reader sees the same snapshot while our write reservation
        # excludes other writers throughout backup, export, and deletion.
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(backup)) as target:
                os.chmod(backup, 0o600)
                source.backup(target)
        with export.open("x", encoding="utf-8") as out:
            os.chmod(export, 0o600)
            json.dump(rows, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())

        conn.executemany("DELETE FROM semantic_entries WHERE id = ? AND key = ?",
                         [(r["id"], r["key"]) for r in rows])
        checks = [r[0] for r in conn.execute("PRAGMA integrity_check")]
        if checks != ["ok"]:
            raise sqlite3.DatabaseError(f"integrity_check failed: {checks}")
        # rank=1 compares the external-content FTS index with its content table.
        conn.execute("INSERT INTO semantic_fts(semantic_fts, rank) VALUES('integrity-check', 1)")
        conn.commit()
        result.update(deleted=len(rows), backup=str(backup), export=str(export),
                      integrity_check="ok", fts_check="ok")
        return result
