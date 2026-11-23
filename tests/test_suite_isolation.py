"""
Suite-level isolation contract: tests never write the live circadian tree.

``autonomy/circadian/{journal,dreams}/`` are the agent's real runtime artifacts
(relative to the repo cwd). Any test that reaches ``append_consolidation_report``
or ``journal_append`` without injecting a dir — e.g. through a daemon hook that
takes no dir argument (the since-retired calibration hook) — used to append fake
``cli`` / ``act-1`` reports into the live dream file (seen 2026-09-13 21:22). ``tests/conftest.py`` redirects both
module defaults to a per-test tmp dir; these tests pin that guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import loom.autonomy.circadian.journal as journal


LIVE_DREAMS = Path("autonomy/circadian/dreams").resolve()
LIVE_JOURNAL = Path("autonomy/circadian/journal").resolve()


def test_default_dirs_redirected_away_from_live_tree(tmp_path):
    assert journal.DEFAULT_DREAMS_DIR.resolve() != LIVE_DREAMS
    assert journal.DEFAULT_JOURNAL_DIR.resolve() != LIVE_JOURNAL
    assert journal.DEFAULT_DREAMS_DIR.is_relative_to(tmp_path)
    assert journal.DEFAULT_JOURNAL_DIR.is_relative_to(tmp_path)


def test_default_append_lands_in_tmp(tmp_path):
    path = journal.append_consolidation_report("isolation probe")
    assert path.resolve().is_relative_to(tmp_path.resolve())


@pytest.mark.live_circadian_dirs
def test_opt_out_marker_keeps_shipped_defaults():
    assert journal.DEFAULT_DREAMS_DIR == Path("autonomy/circadian/dreams")
    assert journal.DEFAULT_JOURNAL_DIR == Path("autonomy/circadian/journal")


# ---------------------------------------------------------------------------
# ~/.loom — the agent's live runtime dir (#690 S4 review)
# ---------------------------------------------------------------------------

import os
import subprocess
import sys

import loom.autonomy.daemon as _daemon
import loom.core.harness.skill_checks as _skill_checks
from loom.core.ledger.store import LedgerStore

_CHILD_ENV = "LOOM_SUITE_ISOLATION_CHILD"


def test_import_bound_live_paths_redirected(tmp_path):
    assert _daemon._CONFIG_HASH_PATH.is_relative_to(tmp_path)
    assert _skill_checks._HASH_DIR.is_relative_to(tmp_path)


def test_live_ledger_db_refused():
    with pytest.raises(RuntimeError, match="live ledger DB"):
        LedgerStore(Path.home() / ".loom" / "ledger.db")
    with pytest.raises(RuntimeError, match="live ledger DB"):
        LedgerStore()  # the default is the live ledger


@pytest.mark.skipif(
    os.environ.get(_CHILD_ENV) == "1", reason="this is the probed suite run itself",
)
def test_whole_suite_leaves_home_loom_untouched(tmp_path):
    """Run the whole suite with ``HOME`` pointed at an empty dir: afterwards
    ``$HOME/.loom`` must hold no file. Every per-test guard above covers one
    path; this catches the next default that resolves under ``~/.loom``
    (seen: schedules.hash, skill_hashes/, ledger.db)."""
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), _CHILD_ENV: "1"}
    repo = Path(__file__).resolve().parents[1]
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=900,
    )
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-2000:]
    written = sorted(
        str(p.relative_to(home)) for p in (home / ".loom").rglob("*") if p.is_file()
    )
    assert written == []
