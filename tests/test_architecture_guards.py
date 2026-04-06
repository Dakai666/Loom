"""
Architecture Guardrail Tests
============================
Static AST analysis — no imports are executed.

Rule: loom/core/ must never import from loom/platform/.
Other forbidden cross-boundary import patterns are listed in FORBIDDEN_PATTERNS.

To intentionally allow a cross-boundary import, add it to ALLOWED_EXCEPTIONS
with a comment explaining why it is safe.
"""

import ast
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# (source_prefix, forbidden_target_prefix)
# Any file whose dotted module path starts with source_prefix must not import
# a module whose dotted path starts with forbidden_target_prefix.
# ---------------------------------------------------------------------------
FORBIDDEN_PATTERNS: list[tuple[str, str]] = [
    # Core business logic must have zero knowledge of platform adapters
    ("loom.core", "loom.platform"),
    ("loom.core.harness", "loom.platform"),
    ("loom.core.memory", "loom.platform"),
    ("loom.core.cognition", "loom.platform"),
    ("loom.core.tasks", "loom.platform"),
    ("loom.core.agent", "loom.platform"),
    # Notify and Autonomy layers must not pull in platform details
    ("loom.notify", "loom.platform"),
    ("loom.autonomy", "loom.platform"),
    # Extensibility is a peer of platform; must not depend on it
    ("loom.extensibility", "loom.platform"),
]

# Deliberate exceptions: (source_file_stem_or_module, imported_module_prefix)
# Add entries here when a cross-boundary import is intentional and reviewed.
ALLOWED_EXCEPTIONS: list[tuple[str, str]] = [
    # Example:
    # ("loom.core.some_module", "loom.platform.cli"),
]


def _module_dotted_path(py_file: Path, loom_root: Path) -> str:
    """Convert an absolute path to a dotted loom.* module path."""
    rel = py_file.relative_to(loom_root.parent)  # relative to project root
    parts = list(rel.with_suffix("").parts)
    return ".".join(parts)


def _get_top_level_imports(py_file: Path) -> list[str]:
    """
    Parse a Python source file and return the fully-qualified module names of
    every `import X` and `from X import ...` statement at any nesting depth.

    Only absolute imports (level == 0) are considered; relative imports are
    always within the same package and cannot violate cross-boundary rules.
    """
    try:
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
    except SyntaxError:
        return []

    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
    return modules


def _is_allowed_exception(source_module: str, imported_module: str) -> bool:
    for src_exc, tgt_exc in ALLOWED_EXCEPTIONS:
        if source_module.startswith(src_exc) and imported_module.startswith(tgt_exc):
            return True
    return False


def test_no_cross_boundary_imports():
    """Fail if any module imports across a forbidden architecture boundary."""
    loom_root = Path(__file__).parent.parent / "loom"
    violations: list[str] = []

    for py_file in sorted(loom_root.rglob("*.py")):
        source_module = _module_dotted_path(py_file, loom_root)
        imported_modules = _get_top_level_imports(py_file)

        for imported in imported_modules:
            for src_prefix, forbidden_prefix in FORBIDDEN_PATTERNS:
                if (
                    source_module.startswith(src_prefix)
                    and imported.startswith(forbidden_prefix)
                    and not _is_allowed_exception(source_module, imported)
                ):
                    violations.append(
                        f"  {source_module}\n"
                        f"    imports {imported!r}  "
                        f"(rule: {src_prefix} → ✗ {forbidden_prefix})"
                    )

    assert not violations, (
        f"{len(violations)} architecture boundary violation(s) found:\n\n"
        + "\n".join(violations)
        + "\n\nTo intentionally allow an import, add it to ALLOWED_EXCEPTIONS "
        "in tests/test_architecture_guards.py with a justification comment."
    )


def test_platform_may_import_core():
    """
    Positive sanity check: platform/ is allowed to import from core/.
    This guards against accidentally over-restricting the FORBIDDEN_PATTERNS.
    """
    loom_root = Path(__file__).parent.parent / "loom"
    platform_files = list((loom_root / "platform").rglob("*.py"))

    # At least one platform file must import from loom.core — otherwise the
    # guardrail matrix is likely misconfigured.
    found_core_import = False
    for py_file in platform_files:
        for imported in _get_top_level_imports(py_file):
            if imported.startswith("loom.core"):
                found_core_import = True
                break
        if found_core_import:
            break

    assert found_core_import, (
        "No platform/ file imports loom.core — "
        "FORBIDDEN_PATTERNS may be misconfigured."
    )
