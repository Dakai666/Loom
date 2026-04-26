"""Startup Diagnostic Suite (Issue #213).

A pluggable health-check system that runs after ``LoomSession.start()``
has wired every subsystem, so operators can see at a glance whether
memory / router / pipeline / skills / registry / config are all healthy
— rather than discovering a bad config when the first turn fails.

Public surface
--------------
- ``StartupDiagnostic``  — runs a list of checks and produces a report.
- ``DiagnosticCheck``    — ABC for individual checks (subclass and
  register to extend).
- ``DiagnosticResult``   — single check outcome.
- ``DiagnosticReport``   — aggregate of all check outcomes; renders.
"""

from loom.core.diagnostic.startup import (
    DiagnosticCheck,
    DiagnosticReport,
    DiagnosticResult,
    StartupDiagnostic,
    default_checks,
)

__all__ = [
    "DiagnosticCheck",
    "DiagnosticReport",
    "DiagnosticResult",
    "StartupDiagnostic",
    "default_checks",
]
