"""
Test-suite conftest.

Pins the user timezone to UTC so timestamp-formatting assertions are
deterministic regardless of which ``loom.toml`` the runner discovers.
Tests that need a different zone should monkeypatch
``loom.core.timezone._USER_ZONE`` themselves.
"""

from __future__ import annotations

import zoneinfo

import loom.core.timezone as _tz_module

_tz_module._USER_ZONE = zoneinfo.ZoneInfo("UTC")


import pytest
from pathlib import Path

import loom.autonomy.circadian.journal as _journal_module


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_circadian_dirs: keep the shipped journal/dreams default dirs "
        "(for tests that assert the defaults themselves; must not write)",
    )


@pytest.fixture(autouse=True)
def _isolate_circadian_dirs(request, tmp_path, monkeypatch):
    """Redirect the circadian journal/dreams default dirs into ``tmp_path``.

    Those defaults are relative to the repo cwd — i.e. the agent's LIVE runtime
    tree. Code paths that take no dir argument (the daemon's
    ``_run_scheduled_*`` hooks) otherwise append fake reports into the real
    dream file on every suite run (#528, seen 2026-09-13).
    """
    if request.node.get_closest_marker("live_circadian_dirs"):
        return
    monkeypatch.setattr(
        _journal_module, "DEFAULT_DREAMS_DIR", tmp_path / "autonomy/circadian/dreams",
    )
    monkeypatch.setattr(
        _journal_module, "DEFAULT_JOURNAL_DIR", tmp_path / "autonomy/circadian/journal",
    )


import loom.core.memory.backup as _backup_module
import loom.core.memory.store as _store_module

_LIVE_LOOM_DIR = (Path.home() / ".loom").resolve()


@pytest.fixture(autouse=True)
def _forbid_live_memory_db(monkeypatch):
    """No test may open a DB under the live ``~/.loom`` dir.

    2026-10-19: a mutation check made a CLI test fall back to the default
    ``~/.loom/memory.db`` with --execute, and a fake LLM rewrote the main
    agent's memory. Whatever a test or a mutation does, this refuses first.
    """
    real_init = _store_module.SQLiteStore.__init__

    def guarded(self, db_path: str = "~/.loom/memory.db") -> None:
        resolved = Path(db_path).expanduser().resolve()
        if resolved == _LIVE_LOOM_DIR or _LIVE_LOOM_DIR in resolved.parents:
            raise RuntimeError(f"test tried to open the live memory DB: {resolved}")
        real_init(self, db_path)

    monkeypatch.setattr(_store_module.SQLiteStore, "__init__", guarded)


@pytest.fixture(autouse=True)
def _isolate_backup_dir(tmp_path, monkeypatch):
    """Memory snapshots (consolidation execute, lifecycle prune) default to
    ``~/.loom/backups`` — the live runtime dir. Keep the suite out of it."""
    monkeypatch.setattr(_backup_module, "DEFAULT_BACKUP_DIR", tmp_path / "backups")
