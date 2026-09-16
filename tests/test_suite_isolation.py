"""
Suite-level isolation contract: tests never write the live circadian tree.

``autonomy/circadian/{journal,dreams}/`` are the agent's real runtime artifacts
(relative to the repo cwd). Any test that reaches ``append_consolidation_report``
or ``journal_append`` without injecting a dir — e.g. through the daemon's
``_run_scheduled_reconcile`` / ``_run_scheduled_calibration`` hooks, which take
no dir argument — used to append fake ``cli`` / ``act-1`` reports into the live
dream file (seen 2026-09-13 21:22, #528). ``tests/conftest.py`` redirects both
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


async def test_scheduled_calibration_hook_does_not_touch_live_dreams(tmp_path):
    """The exact leak path: daemon hook with no dir injection."""
    from loom.autonomy.daemon import _run_scheduled_calibration
    from loom.core.memory.store import SQLiteStore

    store = SQLiteStore(str(tmp_path / "iso.db"))
    await store.initialize()
    async with store.connect() as db:
        await _run_scheduled_calibration(db, enabled=True)

    written = list(journal.DEFAULT_DREAMS_DIR.glob("*.md"))
    assert written and all(p.resolve().is_relative_to(tmp_path.resolve()) for p in written)


@pytest.mark.live_circadian_dirs
def test_opt_out_marker_keeps_shipped_defaults():
    assert journal.DEFAULT_DREAMS_DIR == Path("autonomy/circadian/dreams")
    assert journal.DEFAULT_JOURNAL_DIR == Path("autonomy/circadian/journal")
