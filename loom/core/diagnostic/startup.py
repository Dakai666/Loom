"""Startup Diagnostic Suite — see ``loom.core.diagnostic`` package docstring.

Design notes
------------
- Checks run sequentially. They are cheap (a handle inspection, a count,
  a TOML parse) — never an LLM round-trip — so total cost is sub-ms and
  startup latency is unaffected.
- Each check is independent: a failing check produces a failed
  ``DiagnosticResult`` but does NOT abort the suite; the rest still run.
  Same applies if a check itself raises — the harness wraps it in a
  ``check-raised`` failure so a bad check can never break startup.
- The session stores the produced ``DiagnosticReport`` on
  ``session._startup_report`` so platforms (CLI/TUI/Discord) and
  telemetry can read structured outcomes without re-running checks.
- Pluggability: callers can pass a custom ``checks=[…]`` list to
  ``StartupDiagnostic(...)``; the default set covers core subsystems.

Adding a new check is two lines:

    class MyCheck(DiagnosticCheck):
        name = "my-subsystem"
        async def run(self, session): ...

    StartupDiagnostic(checks=[*default_checks(), MyCheck()])
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class DiagnosticResult:
    """The outcome of a single diagnostic check."""
    name: str
    passed: bool
    summary: str                  # human-readable one-liner ("3 providers", "reachable", …)
    detail: str | None = None     # populated when passed=False

    @property
    def status_glyph(self) -> str:
        return "✓" if self.passed else "✗"


@dataclass
class DiagnosticReport:
    """Aggregate of all check outcomes from one ``StartupDiagnostic.run_all()``."""
    results: list[DiagnosticResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[DiagnosticResult]:
        return [r for r in self.results if not r.passed]

    def render(self) -> str:
        """Return a multi-line, columns-aligned summary suitable for
        console output. Mirrors the format prescribed in #213."""
        if not self.results:
            return "[Loom] Startup diagnostic — no checks registered."

        # Pad name column to widest entry so the glyphs line up.
        width = max(len(r.name) for r in self.results)
        header = "[Loom] Startup diagnostic..."
        body = "\n".join(
            f"  {r.name.ljust(width)}  {r.status_glyph} {r.summary}"
            + (f" — {r.detail}" if r.detail and not r.passed else "")
            for r in self.results
        )
        return f"{header}\n{body}"


# ---------------------------------------------------------------------------
# Check ABC
# ---------------------------------------------------------------------------

class DiagnosticCheck(ABC):
    """Implement a startup health check.

    Subclasses set a class-level ``name`` and implement ``run()``. Any
    exception raised inside ``run()`` is caught by ``StartupDiagnostic``
    and converted to a failed result — implementations should not
    swallow their own errors.
    """
    name: str = ""

    @abstractmethod
    async def run(self, session: Any) -> DiagnosticResult:
        ...


# ---------------------------------------------------------------------------
# Built-in checks
# ---------------------------------------------------------------------------

class MemoryCheck(DiagnosticCheck):
    """Verify the MemoryFacade and its underlying SQLite handle are wired."""
    name = "memory"

    async def run(self, session: Any) -> DiagnosticResult:
        if getattr(session, "_memory", None) is None:
            return DiagnosticResult(
                self.name, False, "facade not built",
                detail="session._memory is None — start() did not complete",
            )
        if getattr(session, "_db", None) is None:
            return DiagnosticResult(
                self.name, False, "DB handle missing",
                detail="session._db is None",
            )
        return DiagnosticResult(self.name, True, "reachable")


class RouterCheck(DiagnosticCheck):
    """Verify the LLM router has at least one provider registered."""
    name = "router"

    async def run(self, session: Any) -> DiagnosticResult:
        router = getattr(session, "router", None)
        if router is None:
            return DiagnosticResult(
                self.name, False, "router missing",
                detail="session.router is None",
            )
        providers = list(getattr(router, "providers", []) or [])
        if not providers:
            return DiagnosticResult(
                self.name, False, "no providers",
                detail="router has zero providers — set MINIMAX_API_KEY / "
                       "ANTHROPIC_API_KEY or configure ollama/lmstudio",
            )
        return DiagnosticResult(
            self.name, True, f"{len(providers)} provider(s) registered",
        )


# Minimum middleware count for a healthy session pipeline. Threshold is
# deliberately conservative — the current production wiring has 7,
# anything below the core 5 means something has been pulled out.
PIPELINE_MIN_MIDDLEWARE = 5


class PipelineCheck(DiagnosticCheck):
    """Verify the middleware pipeline is built and contains the core layers."""
    name = "pipeline"

    async def run(self, session: Any) -> DiagnosticResult:
        pipeline = getattr(session, "_pipeline", None)
        if pipeline is None:
            return DiagnosticResult(
                self.name, False, "pipeline not built",
                detail="session._pipeline is None",
            )

        middlewares = getattr(pipeline, "_middlewares", []) or []
        count = len(middlewares)
        if count < PIPELINE_MIN_MIDDLEWARE:
            return DiagnosticResult(
                self.name, False,
                f"{count} middleware (minimum {PIPELINE_MIN_MIDDLEWARE})",
                detail="core layers may be missing — check session.start()",
            )

        # Spot-check the two layers without which lifecycle / authorization
        # is broken. We use class-name comparison so subclasses still count.
        names = {type(m).__name__ for m in middlewares}
        for required in ("LifecycleMiddleware", "BlastRadiusMiddleware"):
            if required not in names:
                return DiagnosticResult(
                    self.name, False,
                    f"{count} middleware loaded, but {required} missing",
                    detail=f"required middleware '{required}' not in pipeline",
                )

        return DiagnosticResult(self.name, True, f"{count} middleware loaded")


class RegistryCheck(DiagnosticCheck):
    """Verify core tools (write_file / read_file / run_bash / list_dir) are
    registered. Other tools are inspectable via the count."""
    name = "registry"

    REQUIRED_TOOLS = ("write_file", "read_file", "run_bash", "list_dir")

    async def run(self, session: Any) -> DiagnosticResult:
        registry = getattr(session, "registry", None)
        if registry is None:
            return DiagnosticResult(
                self.name, False, "registry missing",
                detail="session.registry is None",
            )

        missing = [
            name for name in self.REQUIRED_TOOLS if registry.get(name) is None
        ]
        if missing:
            return DiagnosticResult(
                self.name, False,
                f"{len(missing)} core tool(s) missing",
                detail=f"missing: {', '.join(missing)}",
            )

        total = len(getattr(registry, "_tools", {}) or {})
        return DiagnosticResult(
            self.name, True, f"{total} tool(s) registered",
        )


class SkillsCheck(DiagnosticCheck):
    """Report indexed skill count from the MemoryIndex.

    Zero skills is *not* a failure — fresh installs and unit-test
    sessions legitimately have none. We only fail if the index itself
    failed to build.

    Reads ``MemoryIndex.skill_count`` (the field the indexer actually
    populates from ``ProceduralMemory.list_active()``). An earlier
    version of this check read a non-existent ``.skills`` attribute via
    ``getattr(..., "skills", [])`` and silently always reported zero —
    masking real skill counts and creating the false appearance that
    ``_auto_import_skills`` had stopped registering SkillGenomes.
    """
    name = "skills"

    async def run(self, session: Any) -> DiagnosticResult:
        index = getattr(session, "_memory_index", None)
        if index is None:
            return DiagnosticResult(
                self.name, False, "MemoryIndex not built",
                detail="session._memory_index is None",
            )
        count = int(getattr(index, "skill_count", 0) or 0)
        if count == 0:
            return DiagnosticResult(self.name, True, "0 skills indexed")
        return DiagnosticResult(
            self.name, True, f"{count} skill(s) indexed",
        )


class ConfigCheck(DiagnosticCheck):
    """Verify ``loom.toml`` parses (or is absent — both are fine).

    A *malformed* loom.toml is the failure case; a missing one is OK
    because Loom ships sensible defaults.
    """
    name = "config"

    async def run(self, session: Any) -> DiagnosticResult:
        # Re-import each time so tests can monkey-patch ``_load_loom_config``
        # without us caching its successful return value at import time.
        from loom.core import session as session_module

        try:
            cfg = session_module._load_loom_config()
        except Exception as exc:
            return DiagnosticResult(
                self.name, False, "loom.toml parse failed",
                detail=f"{type(exc).__name__}: {exc}",
            )

        if not cfg:
            return DiagnosticResult(self.name, True, "no loom.toml (defaults)")
        return DiagnosticResult(
            self.name, True, f"{len(cfg)} top-level section(s) parsed",
        )


def default_checks() -> list[DiagnosticCheck]:
    """The standard suite, in execution order. Order matters for
    rendering — bottom-up dependencies first, so a memory failure
    appears above (and explains) downstream failures."""
    return [
        MemoryCheck(),
        RouterCheck(),
        PipelineCheck(),
        RegistryCheck(),
        SkillsCheck(),
        ConfigCheck(),
    ]


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------

class StartupDiagnostic:
    """Run a list of ``DiagnosticCheck`` against a ``LoomSession``.

    Usage (typically called by ``LoomSession.start()`` itself):

        report = await StartupDiagnostic().run_all(session)
        if not report.all_passed:
            logger.warning("Startup diagnostic flagged %d failures",
                           len(report.failures))
    """

    def __init__(self, checks: list[DiagnosticCheck] | None = None) -> None:
        self._checks = list(checks) if checks is not None else default_checks()

    async def run_all(self, session: Any) -> DiagnosticReport:
        results: list[DiagnosticResult] = []
        for check in self._checks:
            results.append(await self._run_one(check, session))
        return DiagnosticReport(results=results)

    async def _run_one(
        self, check: DiagnosticCheck, session: Any,
    ) -> DiagnosticResult:
        """Run one check with full failure isolation — a buggy custom
        check never breaks startup."""
        try:
            return await check.run(session)
        except Exception as exc:
            logger.warning(
                "Diagnostic check %r raised: %s", check.name, exc,
                exc_info=True,
            )
            return DiagnosticResult(
                check.name or type(check).__name__,
                False,
                "check raised",
                detail=f"{type(exc).__name__}: {exc}",
            )
