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


@pytest.fixture(autouse=True)
def _isolate_log_once(monkeypatch):
    """Each test represents a fresh process; repeat loads within it dedup."""
    import importlib
    module = importlib.import_module("loom.core.infra.log_once")
    monkeypatch.setattr(module, "_seen", set())


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


import loom.core.cognition.distillation as _distillation_module
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


import loom.core.harness.skill_checks as _skill_checks_module
import loom.core.ledger.store as _ledger_store_module


@pytest.fixture(autouse=True)
def _forbid_live_ledger_db(monkeypatch):
    """No test may open the live ``~/.loom/ledger.db``.

    Same guard as the memory DB. ``LedgerStore()`` defaults to the live
    ledger, and ``LoomSession.start()`` and ``loom ledger`` build one with
    no path: a test that reaches them and emits would write rows into the
    agent's real ledger.
    """
    real_init = _ledger_store_module.LedgerStore.__init__

    def guarded(self, db_path=None, blob_dir=None) -> None:
        real_init(self, db_path, blob_dir)
        resolved = self.db_path.expanduser().resolve()
        if resolved == _LIVE_LOOM_DIR or _LIVE_LOOM_DIR in resolved.parents:
            raise RuntimeError(f"test tried to open the live ledger DB: {resolved}")

    monkeypatch.setattr(_ledger_store_module.LedgerStore, "__init__", guarded)


@pytest.fixture(autouse=True)
def _isolate_skill_hash_dir(tmp_path, monkeypatch):
    """Skill check hashes default to ``~/.loom/skill_hashes`` (bound at
    import); tests left ``test_resolve_*`` files in the live dir."""
    monkeypatch.setattr(_skill_checks_module, "_HASH_DIR", tmp_path / "skill_hashes")


# Every provider credential ``build_router`` can read from the process
# environment as well as from .env (#661).
PROVIDER_ENV = (
    "MINIMAX_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY", "OLLAMA_BASE_URL", "LMSTUDIO_BASE_URL",
)


@pytest.fixture(autouse=True)
def _no_ambient_provider_env(monkeypatch):
    """A test states its providers; none leak in from the developer's shell.

    #661: build_router falls back to ``os.environ`` for each key, so a key
    exported in the shell registered providers the test never asked for.
    """
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolate_backup_dir(tmp_path, monkeypatch):
    """Memory snapshots (consolidation execute, lifecycle prune) default to
    ``~/.loom/backups`` — the live runtime dir. Keep the suite out of it."""
    monkeypatch.setattr(_backup_module, "DEFAULT_BACKUP_DIR", tmp_path / "backups")


@pytest.fixture(autouse=True)
def _isolate_schedule_hash(tmp_path, monkeypatch):
    """``AutonomyDaemon.load_config`` records the schedule-registry hash in
    ``~/.loom/schedules.hash`` (#91 tamper detection). A test's registry
    overwrote the live hash, so the real daemon's next start warned of a
    change that never happened."""
    import loom.autonomy.daemon as _daemon_module

    monkeypatch.setattr(_daemon_module, "_CONFIG_HASH_PATH", tmp_path / "schedules.hash")


@pytest.fixture(autouse=True)
def _isolate_distill_debug_dir(tmp_path, monkeypatch):
    """Unparseable distill replies are dumped to ``~/.loom/debug`` (#629)."""
    monkeypatch.setattr(_distillation_module, "DEFAULT_DEBUG_DIR", tmp_path / "debug")
