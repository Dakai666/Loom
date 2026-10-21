"""``loom memory big-dream`` — writes only with --execute AND the loom.toml gates."""

from __future__ import annotations

import asyncio
import types

import pytest
from click.testing import CliRunner

import loom.platform.cli.main as cli_main
from loom.core.memory.store import SQLiteStore
from tests.test_big_dream import _dream_llm
from tests.test_distill_dream import _topic


def _seed(path: str) -> None:
    async def go():
        store = SQLiteStore(path)
        await store.initialize()
        async with store.connect() as db:
            await _topic(db, "a", 0)
    asyncio.run(go())


def _stubs(path: str) -> int:
    import sqlite3
    with sqlite3.connect(path) as c:
        return c.execute("SELECT count(*) FROM semantic_entries "
                         "WHERE json_extract(metadata,'$.redirected_to') IS NOT NULL").fetchone()[0]


@pytest.fixture
def run(tmp_path, monkeypatch):
    db = str(tmp_path / "m.db")
    _seed(db)
    llm = _dream_llm()

    class Router:
        async def chat(self, *, model, messages, max_tokens):
            return types.SimpleNamespace(text=await llm(messages), stop_reason="end_turn")

    monkeypatch.chdir(tmp_path)                        # journal + cwd loom.toml land here
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # a default path can't reach ~/.loom
    monkeypatch.setattr(cli_main, "build_router", lambda model: Router())
    monkeypatch.setattr("loom.core.memory.embeddings.build_embedding_provider",
                        lambda env, cfg: None)
    monkeypatch.setattr("loom.core.cognition.router.get_default_model", lambda: "m")

    def invoke(args, cfg, *, pass_db=True):
        monkeypatch.setattr(cli_main, "_load_loom_config", lambda: cfg)
        db_args = ["--db", db] if pass_db else []
        return CliRunner().invoke(cli_main.cli, ["memory", "big-dream", *db_args, *args])
    return invoke, db


GATES_ON = {"memory": {"consolidation_dream": {"execute": True},
                       "distill_dream": {"execute": True}}}


def test_default_is_read_only(run):
    invoke, db = run
    res = invoke([], GATES_ON)
    assert res.exit_code == 0, res.output
    assert "read-only" in res.output
    assert _stubs(db) == 0


def test_execute_without_gates_stays_read_only(run):
    invoke, db = run
    res = invoke(["--execute", "--yes"], {})
    assert res.exit_code == 0, res.output
    assert "[memory.distill_dream] execute is not true" in res.output
    assert _stubs(db) == 0


def test_execute_with_gates_writes(run):
    invoke, db = run
    res = invoke(["--execute", "--yes"], GATES_ON)
    assert res.exit_code == 0, res.output
    assert _stubs(db) == 2
    assert "mode: execute" in res.output


def test_execute_asks_first_without_yes(run):
    invoke, db = run
    res = invoke(["--execute"], GATES_ON)     # CliRunner feeds no input → declined
    assert "Aborted" in res.output
    assert _stubs(db) == 0


def test_db_defaults_to_the_configs_db_path(run, tmp_path):
    # run from a second agent's directory without --db: its own DB, not ~/.loom
    invoke, db = run
    cfg = {"memory": {"db_path": "m.db", **GATES_ON["memory"]}}
    res = invoke(["--execute", "--yes"], cfg, pass_db=False)
    assert res.exit_code == 0, res.output
    assert _stubs(db) == 2


def test_the_live_db_is_unreachable_from_tests():
    # conftest's guard: the 2026-10-19 accident can't repeat
    from pathlib import Path

    from loom.core.memory.store import SQLiteStore
    with pytest.raises(RuntimeError, match="live memory DB"):
        SQLiteStore(str(Path(__file__).home() / ".loom" / "memory.db"))
