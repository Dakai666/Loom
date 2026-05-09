"""#342 — POSIX file-permission baseline for ~/.loom/.

Tests the helper plus its integration into SQLiteStore.initialize and
LedgerStore.open. POSIX-only; the helpers are no-ops on Windows so the
non-POSIX cases pass through trivially.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import pytest_asyncio

from loom.core.infra import file_permissions as fp


posix_only = pytest.mark.skipif(
    os.name != "posix", reason="POSIX file permissions only"
)


@pytest.fixture(autouse=True)
def _reset_warning():
    """Each test starts with a fresh warning flag — the helper warns
    at most once per process and we don't want one test to mask another."""
    fp.reset_warning_state_for_tests()
    yield
    fp.reset_warning_state_for_tests()


# ---------------------------------------------------------------------------
# Unit — helper itself
# ---------------------------------------------------------------------------


@posix_only
def test_tighten_file_locks_loose_perms(tmp_path: Path) -> None:
    f = tmp_path / "x.db"
    f.write_text("secret")
    os.chmod(f, 0o644)
    fp.tighten_file(f)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


@posix_only
def test_tighten_file_idempotent(tmp_path: Path, caplog) -> None:
    f = tmp_path / "x.db"
    f.write_text("secret")
    os.chmod(f, 0o600)
    with caplog.at_level("WARNING", logger="loom.core.infra.file_permissions"):
        fp.tighten_file(f)
        fp.tighten_file(f)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    # Already-tight files don't trigger the warning.
    assert not any("Tightened loose" in r.message for r in caplog.records)


def test_tighten_file_missing_is_silent(tmp_path: Path) -> None:
    fp.tighten_file(tmp_path / "does-not-exist")
    # Just shouldn't raise.


@posix_only
def test_tighten_dir_locks_loose_perms(tmp_path: Path) -> None:
    d = tmp_path / "store"
    d.mkdir()
    os.chmod(d, 0o755)
    fp.tighten_dir(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


@posix_only
def test_warning_fires_once_then_silent(tmp_path: Path, caplog) -> None:
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    a.write_text("x")
    b.write_text("y")
    os.chmod(a, 0o644)
    os.chmod(b, 0o644)
    with caplog.at_level("WARNING", logger="loom.core.infra.file_permissions"):
        fp.tighten_file(a)
        fp.tighten_file(b)
    warns = [r for r in caplog.records if "Tightened loose" in r.message]
    assert len(warns) == 1


@posix_only
def test_tighten_sqlite_db_covers_wal_shm_journal(tmp_path: Path) -> None:
    db = tmp_path / "session.db"
    wal = tmp_path / "session.db-wal"
    shm = tmp_path / "session.db-shm"
    journal = tmp_path / "session.db-journal"
    for f in (db, wal, shm, journal):
        f.write_text("x")
        os.chmod(f, 0o644)
    fp.tighten_sqlite_db(db)
    for f in (db, wal, shm, journal):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f"{f} not tightened"


# ---------------------------------------------------------------------------
# Integration — SQLiteStore + LedgerStore
# ---------------------------------------------------------------------------


@posix_only
async def test_sqlite_store_tightens_dir_and_file(tmp_path: Path) -> None:
    from loom.core.memory.store import SQLiteStore

    db_path = tmp_path / "store" / "memory.db"
    store = SQLiteStore(str(db_path))
    # Pre-create the parent dir loose so we exercise the tighten path.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(db_path.parent, 0o755)

    await store.initialize()

    assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


@posix_only
async def test_ledger_store_tightens_dir_blob_and_file(tmp_path: Path) -> None:
    from loom.core.ledger import LedgerStore

    db_path = tmp_path / "ledger.db"
    blob_dir = tmp_path / "ledger_blobs"
    store = LedgerStore(db_path=db_path, blob_dir=blob_dir)
    # blob_dir doesn't exist yet — open() creates it.
    await store.open()
    try:
        assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(blob_dir.stat().st_mode) == 0o700
    finally:
        await store.close()
