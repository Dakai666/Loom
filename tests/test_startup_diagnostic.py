"""Issue #213: Startup Diagnostic Suite.

Pin the public contract of the diagnostic module:

* Built-in checks return structured ``DiagnosticResult`` with the
  expected pass/fail semantics.
* The suite isolates check failures — one bad check never aborts the
  rest, and a check that *raises* still produces a graceful failed
  result instead of bubbling out.
* Custom ``DiagnosticCheck`` subclasses can be plugged in.
* ``DiagnosticReport.render()`` emits the columns-aligned format
  prescribed in the issue.
* ``LoomSession.start()`` populates ``session._startup_report`` so
  platforms / telemetry can read structured outcomes.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from loom.core.diagnostic import (
    DiagnosticCheck,
    DiagnosticReport,
    DiagnosticResult,
    StartupDiagnostic,
    default_checks,
)
from loom.core.diagnostic.startup import (
    ConfigCheck,
    MemoryCheck,
    PipelineCheck,
    RegistryCheck,
    RouterCheck,
    SkillsCheck,
)


# ---------------------------------------------------------------------------
# Fake session — minimum surface the built-in checks read.
# ---------------------------------------------------------------------------

def _fake_session(
    *,
    memory: bool = True,
    db: bool = True,
    router_providers: int = 2,
    middleware_count: int = 7,
    middleware_classes: tuple[str, ...] = (
        "JITRetrievalMiddleware",
        "LifecycleMiddleware",
        "TraceMiddleware",
        "SchemaValidationMiddleware",
        "BlastRadiusMiddleware",
        "LegitimacyGuard",
        "LifecycleGateMiddleware",
    ),
    tools: tuple[str, ...] = ("write_file", "read_file", "run_bash", "list_dir"),
    skills_count: int = 3,
) -> SimpleNamespace:
    """Build a session-shaped object with just the attributes the
    diagnostic checks consult. Keeping this synthetic avoids the
    expensive LoomSession.start() path in unit tests."""
    middlewares = [
        type(name, (), {})() for name in middleware_classes[:middleware_count]
    ]
    pipeline = SimpleNamespace(_middlewares=middlewares)

    registry_tools = {name: object() for name in tools}
    registry = MagicMock()
    registry.get.side_effect = lambda n: registry_tools.get(n)
    registry._tools = registry_tools

    return SimpleNamespace(
        _memory=object() if memory else None,
        _db=object() if db else None,
        router=SimpleNamespace(
            providers=[object() for _ in range(router_providers)],
        ),
        _pipeline=pipeline,
        registry=registry,
        # Mirror the real ``MemoryIndex`` field names. An earlier version of
        # the SkillsCheck read a non-existent ``.skills`` attribute and the
        # fake here mirrored the same typo, so the test silently passed
        # while the real check always reported zero.
        _memory_index=SimpleNamespace(
            skill_count=skills_count,
            skill_catalog=[f"s{i}" for i in range(skills_count)],
        ),
    )


# ---------------------------------------------------------------------------
# Result / Report shape
# ---------------------------------------------------------------------------

class TestReportShape:
    def test_status_glyph_pass(self) -> None:
        r = DiagnosticResult("x", True, "ok")
        assert r.status_glyph == "✓"

    def test_status_glyph_fail(self) -> None:
        r = DiagnosticResult("x", False, "no", detail="reason")
        assert r.status_glyph == "✗"

    def test_all_passed(self) -> None:
        rpt = DiagnosticReport([
            DiagnosticResult("a", True, "ok"),
            DiagnosticResult("b", True, "ok"),
        ])
        assert rpt.all_passed
        assert rpt.failures == []

    def test_failures_listed(self) -> None:
        bad = DiagnosticResult("b", False, "boom", detail="stack")
        rpt = DiagnosticReport([
            DiagnosticResult("a", True, "ok"),
            bad,
        ])
        assert not rpt.all_passed
        assert rpt.failures == [bad]

    def test_render_aligns_columns_and_shows_detail_only_for_failures(self) -> None:
        rpt = DiagnosticReport([
            DiagnosticResult("memory", True, "reachable"),
            DiagnosticResult("router", False, "no providers", detail="set keys"),
        ])
        out = rpt.render()
        assert "[Loom] Startup diagnostic..." in out
        assert "memory  ✓ reachable" in out
        # Failure includes the ` — detail` suffix.
        assert "router  ✗ no providers — set keys" in out


# ---------------------------------------------------------------------------
# Built-in checks
# ---------------------------------------------------------------------------

class TestBuiltinChecks:
    @pytest.mark.asyncio
    async def test_memory_check_passes_when_facade_and_db_present(self) -> None:
        r = await MemoryCheck().run(_fake_session())
        assert r.passed
        assert r.summary == "reachable"

    @pytest.mark.asyncio
    async def test_memory_check_fails_without_facade(self) -> None:
        r = await MemoryCheck().run(_fake_session(memory=False))
        assert not r.passed
        assert "facade not built" in r.summary

    @pytest.mark.asyncio
    async def test_router_check_fails_with_zero_providers(self) -> None:
        r = await RouterCheck().run(_fake_session(router_providers=0))
        assert not r.passed
        assert "no providers" in r.summary
        assert "MINIMAX_API_KEY" in r.detail

    @pytest.mark.asyncio
    async def test_pipeline_check_passes_with_full_set(self) -> None:
        r = await PipelineCheck().run(_fake_session())
        assert r.passed
        assert "7 middleware" in r.summary

    @pytest.mark.asyncio
    async def test_pipeline_check_fails_when_below_minimum(self) -> None:
        # Only 2 middleware → below PIPELINE_MIN_MIDDLEWARE (5).
        r = await PipelineCheck().run(
            _fake_session(middleware_count=2),
        )
        assert not r.passed
        assert "minimum 5" in r.summary

    @pytest.mark.asyncio
    async def test_pipeline_check_fails_when_required_layer_missing(self) -> None:
        # Five layers but BlastRadiusMiddleware is replaced with a stub.
        r = await PipelineCheck().run(_fake_session(
            middleware_classes=(
                "JITRetrievalMiddleware",
                "LifecycleMiddleware",
                "TraceMiddleware",
                "SchemaValidationMiddleware",
                "LifecycleGateMiddleware",
            ),
            middleware_count=5,
        ))
        assert not r.passed
        assert "BlastRadiusMiddleware" in r.summary

    @pytest.mark.asyncio
    async def test_registry_check_fails_when_core_tool_missing(self) -> None:
        r = await RegistryCheck().run(_fake_session(
            tools=("write_file", "read_file"),  # missing run_bash + list_dir
        ))
        assert not r.passed
        assert "missing: run_bash, list_dir" in r.detail

    @pytest.mark.asyncio
    async def test_skills_check_passes_with_zero_skills(self) -> None:
        """Empty skill set is legal (fresh install / unit test)."""
        r = await SkillsCheck().run(_fake_session(skills_count=0))
        assert r.passed
        assert "0 skills" in r.summary

    @pytest.mark.asyncio
    async def test_skills_check_reports_real_count(self) -> None:
        """Regression: prior to the fix, SkillsCheck read a non-existent
        ``index.skills`` attribute via ``getattr(..., "skills", [])`` and
        always reported zero, masking the real skill count loaded by
        ``_auto_import_skills``. This pins the contract that the check
        reads ``skill_count`` and surfaces the actual number."""
        r = await SkillsCheck().run(_fake_session(skills_count=10))
        assert r.passed
        assert "10 skill(s) indexed" in r.summary

    @pytest.mark.asyncio
    async def test_skills_check_uses_real_memoryindex_field(self) -> None:
        """Regression: a pure ``MemoryIndex`` (no ``skills`` attribute,
        only ``skill_count`` / ``skill_catalog``) must be readable by
        SkillsCheck. This catches the original typo where the check
        consulted an attribute name that ``MemoryIndex`` does not
        define — the assertion below would fail under the old code."""
        from loom.core.memory.index import MemoryIndex

        index = MemoryIndex(skill_count=4, skill_catalog=[])
        session = SimpleNamespace(_memory_index=index)
        r = await SkillsCheck().run(session)
        assert r.passed
        assert "4 skill(s) indexed" in r.summary

    @pytest.mark.asyncio
    async def test_config_check_passes_when_config_loads(self, monkeypatch) -> None:
        from loom.core import session as session_module
        monkeypatch.setattr(session_module, "_load_loom_config", lambda: {"x": 1})
        r = await ConfigCheck().run(_fake_session())
        assert r.passed
        assert "1 top-level section" in r.summary

    @pytest.mark.asyncio
    async def test_config_check_fails_when_parse_raises(self, monkeypatch) -> None:
        from loom.core import session as session_module

        def _bad():
            raise ValueError("malformed toml at line 4")

        monkeypatch.setattr(session_module, "_load_loom_config", _bad)
        r = await ConfigCheck().run(_fake_session())
        assert not r.passed
        assert "loom.toml parse failed" in r.summary
        assert "ValueError" in r.detail


# ---------------------------------------------------------------------------
# Suite isolation & pluggability
# ---------------------------------------------------------------------------

class _RaisingCheck(DiagnosticCheck):
    name = "raises"

    async def run(self, session):
        raise RuntimeError("custom check exploded")


class _SimpleCheck(DiagnosticCheck):
    name = "custom"

    async def run(self, session):
        return DiagnosticResult(self.name, True, "ran")


class TestSuiteIsolationAndPluggability:
    @pytest.mark.asyncio
    async def test_check_that_raises_does_not_abort_suite(self) -> None:
        """A buggy custom check must not break startup — the harness
        catches it and continues to the next check."""
        suite = StartupDiagnostic(checks=[_RaisingCheck(), _SimpleCheck()])
        report = await suite.run_all(_fake_session())

        assert len(report.results) == 2
        # The raising check produced a graceful failed result …
        raised = report.results[0]
        assert raised.name == "raises"
        assert not raised.passed
        assert raised.summary == "check raised"
        assert "RuntimeError" in raised.detail
        assert "custom check exploded" in raised.detail
        # … and the next check still ran.
        assert report.results[1].passed
        assert report.results[1].name == "custom"

    @pytest.mark.asyncio
    async def test_default_checks_run_against_fake_session(self) -> None:
        """End-to-end smoke: the default suite runs cleanly against a
        properly-shaped session."""
        # Patch _load_loom_config so the config check has a stable answer.
        from loom.core import session as session_module
        orig = session_module._load_loom_config
        session_module._load_loom_config = lambda: {"k": "v"}
        try:
            suite = StartupDiagnostic()  # uses default_checks()
            report = await suite.run_all(_fake_session())
        finally:
            session_module._load_loom_config = orig

        # Every default check should produce a result.
        names = [r.name for r in report.results]
        for expected in ("memory", "router", "pipeline", "registry", "skills", "config"):
            assert expected in names
        assert report.all_passed, [
            (r.name, r.summary, r.detail) for r in report.failures
        ]

    def test_default_checks_returns_independent_lists(self) -> None:
        """Mutating one caller's list should not poison the next."""
        a = default_checks()
        b = default_checks()
        a.clear()
        assert len(b) > 0


# ---------------------------------------------------------------------------
# Integration: LoomSession.start() populates _startup_report
# ---------------------------------------------------------------------------

class TestSessionIntegration:
    @pytest.mark.asyncio
    async def test_start_populates_startup_report(
        self, monkeypatch, tmp_path,
    ) -> None:
        """After ``LoomSession.start()`` returns, ``_startup_report`` is
        a populated DiagnosticReport — proving the suite ran end-to-end
        against the real wiring."""
        from loom.core import session as session_module
        from loom.core.session import LoomSession

        workspace = tmp_path / "ws"
        workspace.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(session_module, "build_router", lambda: MagicMock(providers=[object()]))
        monkeypatch.setattr(session_module, "_load_loom_config", lambda: {})
        monkeypatch.setattr(session_module, "_load_env", lambda project_root=None: {})
        monkeypatch.setattr(session_module, "build_embedding_provider", lambda env, cfg: None)
        from rich.prompt import Confirm
        monkeypatch.setattr(Confirm, "ask", lambda *args, **kwargs: True)

        session = LoomSession(
            model="test-model",
            db_path=str(tmp_path / "loom.db"),
            workspace=workspace,
        )
        # Default value before start().
        assert session._startup_report is None

        await session.start()
        try:
            report = session._startup_report
            assert report is not None
            assert isinstance(report, DiagnosticReport)
            # Every default check must appear, regardless of pass/fail.
            names = [r.name for r in report.results]
            for expected in ("memory", "router", "pipeline", "registry", "skills", "config"):
                assert expected in names
        finally:
            await session.stop()
