"""#639 (folded into #650) — the dream agent tools are retired.

Dreaming is the daemon schedules' job; Loom Agent decides what to remember.

Contract:
* a started session registers none of ``dream_cycle`` / ``convergent_dream`` /
  ``distill_dream`` (``memory_prune`` is still wired);
* the divergent-dream module ``loom.core.cognition.dreaming`` is gone, and no
  tool factory for the three is left in ``loom.core.memory.maintenance``;
* the distill dream's themed-domain rotation lives in ``distillation`` and
  still round-robins self → user → project → knowledge under its own key;
* both daemon loops still start under the live config shape (execute gates
  read as before).
"""

import importlib
import types
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from loom.core.memory.store import SQLiteStore

RETIRED_TOOLS = ("dream_cycle", "convergent_dream", "distill_dream")


@pytest_asyncio.fixture
async def db(tmp_path):
    store = SQLiteStore(str(tmp_path / "retired.db"))
    await store.initialize()
    async with store.connect() as conn:
        yield conn


def test_dreaming_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("loom.core.cognition.dreaming")


def test_no_dream_exports_or_factories_left():
    import loom.core.cognition as cognition
    import loom.core.memory.maintenance as maintenance

    assert not hasattr(cognition, "dream_cycle")
    for name in ("make_dream_cycle_tool", "make_convergent_dream_tool",
                 "make_distill_dream_tool"):
        assert not hasattr(maintenance, name), name
    # kept: the CLI's big-dream and the daemon read this gate
    assert maintenance.distill_execute_enabled({"memory": {"distill_dream": {"execute": True}}})


async def test_session_registers_no_dream_tools(monkeypatch, tmp_path):
    from loom.core import session as core_session
    from loom.core.session import LoomSession

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(core_session, "build_router", lambda *a, **k: MagicMock())
    monkeypatch.setattr(core_session, "_load_loom_config", lambda: {"memory": {
        "distill_dream": {"enabled": True, "execute": True},
        "consolidation_dream": {"enabled": True, "execute": True},
    }})
    monkeypatch.setattr(core_session, "_load_env", lambda project_root=None: {})
    monkeypatch.setattr(core_session, "build_embedding_provider", lambda env, cfg: None)
    from rich.prompt import Confirm

    monkeypatch.setattr(Confirm, "ask", lambda *args, **kwargs: True)

    session = LoomSession(model="gpt-test", db_path=str(tmp_path / "loom.db"),
                          workspace=workspace)
    await session.start()
    try:
        for name in RETIRED_TOOLS:
            assert session.registry.get(name) is None, name
        assert session.registry.get("memory_prune") is not None
    finally:
        await session.stop()


async def test_distill_theme_rotation_lives_in_distillation(db):
    from loom.core.cognition.distillation import mark_themed_domain, peek_themed_domain

    key = "distill_dream.last_theme"
    seen = []
    for _ in range(5):
        domain = await peek_themed_domain(db, key)
        assert await peek_themed_domain(db, key) == domain   # peek never advances
        await mark_themed_domain(db, key, domain)
        seen.append(domain)
    assert seen == ["self", "user", "project", "knowledge", "self"]


# ── both daemon loops still start under the live config ────────────────────

LIVE_MEMORY_CFG = {
    "consolidation_dream": {"enabled": True, "interval_days": 7, "execute": True,
                            "corpus_limit": 15000, "max_clusters": 40,
                            "max_review_clusters": 40},
    "distill_dream": {"enabled": True, "interval_hours": 12, "execute": True,
                      "max_neighbourhoods": 5, "min_similarity": 0.70,
                      "corpus_limit": 15000},
}


def _daemon_with_fake_loop(monkeypatch, db, holders):
    import loom.autonomy.daemon as daemon_mod
    import loom.core.config as config_mod
    from loom.autonomy.daemon import AutonomyDaemon

    monkeypatch.setattr(config_mod, "load_loom_config", lambda: {"memory": LIVE_MEMORY_CFG})

    class FakeLoop:
        def __init__(self, **kw):
            holders.append(kw)
            self._interval_seconds = kw.get("interval_days", 7.0) * 86400.0

        def run_forever(self):
            async def _noop():
                return None
            return _noop()

    monkeypatch.setattr(daemon_mod, "ConvergentDreamLoop", FakeLoop)
    session = types.SimpleNamespace(_db=db, _memory=types.SimpleNamespace(semantic=object()),
                                    router=object(), model="m")
    return AutonomyDaemon(notify_router=None, confirm_flow=None, loom_session=session)


async def test_both_daemon_loops_start_under_live_config(monkeypatch, db):
    import loom.core.cognition.distillation as distill_mod

    holders: list[dict] = []
    daemon = _daemon_with_fake_loop(monkeypatch, db, holders)
    consolidation = daemon._maybe_start_consolidation_loop()
    distill = daemon._maybe_start_dream_loop()
    assert consolidation is not None and distill is not None
    await consolidation
    await distill
    conv_kw, dist_kw = holders
    assert conv_kw.get("meta_key", "consolidation_dream.last_run") == "consolidation_dream.last_run"
    assert conv_kw["interval_days"] == pytest.approx(7.0)
    assert dist_kw["meta_key"] == "distill_dream.last_run"
    assert dist_kw["interval_days"] == pytest.approx(0.5)

    # The distill closure seeds from the rotation now living in distillation
    # and passes the execute gate through unchanged.
    captured: list[dict] = []

    async def fake_run(semantic, llm_fn, **kw):
        captured.append(kw)
        plan = types.SimpleNamespace(pass_id="p", domain=kw["domain"], mode="read-only",
                                     counts=lambda: {}, outcome_counts=lambda: {})
        return plan, "report"

    import loom.autonomy.circadian.journal as journal_mod
    monkeypatch.setattr(distill_mod, "run_distill_dream", fake_run)
    monkeypatch.setattr(journal_mod, "append_distill_report", lambda report, **_: None)
    await dist_kw["dream_fn"]()
    await dist_kw["dream_fn"]()
    assert [c["domain"] for c in captured] == ["self", "user"]
    assert all(c["execute"] is True for c in captured)
