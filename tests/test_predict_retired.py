"""#674 — `predict` is retired.

Contract:
* the predict modules are gone (no import path left to wire back in);
* a session started with the old ``predict_tool_enabled = true`` config does
  not register ``predict`` and does not fail;
* an older ``memory.db`` that still holds ``prediction_records`` opens fine and
  its rows are left untouched; a fresh DB no longer creates the table.
"""

import importlib
import sqlite3
from unittest.mock import MagicMock

import pytest

from loom.core.memory.store import SQLiteStore


@pytest.mark.parametrize(
    "module",
    [
        "loom.core.memory.prediction",
        "loom.core.memory.prediction_settle",
        "loom.core.memory.resolvers",
    ],
)
def test_predict_modules_are_gone(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_session_support_has_no_betting_nudge():
    from loom.core import session_support

    assert not hasattr(session_support, "prediction_nudge_body")


def _tables(path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


async def test_fresh_db_has_no_prediction_table(tmp_path):
    path = tmp_path / "fresh.db"
    await SQLiteStore(str(path)).initialize()
    assert "prediction_records" not in _tables(path)


async def test_legacy_db_with_prediction_rows_opens_and_keeps_them(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE prediction_records (id TEXT PRIMARY KEY, claim TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO prediction_records VALUES ('p1', 'old bet')")

    await SQLiteStore(str(path)).initialize()

    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT id, claim FROM prediction_records").fetchall()
    assert rows == [("p1", "old bet")]


async def test_old_predict_config_is_ignored_and_tool_not_registered(
    monkeypatch, tmp_path
):
    from loom.core import session as core_session
    from loom.core.session import LoomSession

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(core_session, "build_router", lambda *a, **k: MagicMock())
    monkeypatch.setattr(
        core_session,
        "_load_loom_config",
        lambda: {"memory": {"consolidation_dream": {"predict_tool_enabled": True}}},
    )
    monkeypatch.setattr(core_session, "_load_env", lambda project_root=None: {})
    monkeypatch.setattr(
        core_session, "build_embedding_provider", lambda env, cfg: None
    )
    from rich.prompt import Confirm

    monkeypatch.setattr(Confirm, "ask", lambda *args, **kwargs: True)

    session = LoomSession(
        model="gpt-test", db_path=str(tmp_path / "loom.db"), workspace=workspace
    )
    await session.start()
    try:
        assert session.registry.get("predict") is None
        assert session.registry.get("memory_prune") is not None  # sibling still wired
    finally:
        await session.stop()
