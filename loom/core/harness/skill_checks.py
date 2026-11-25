"""
Skill Precondition Check Manager — Issue #64 Phase B.

Dynamically mounts/unmounts skill-declared precondition checks onto
ToolDefinitions at ``load_skill()`` time.

Design:
    - Each skill's SKILL.md frontmatter can declare ``precondition_checks``
      referencing Python callables in the skill's directory.
    - On ``load_skill()``, callables are resolved (imported) and appended
      to the target ToolDefinition.precondition_checks list.
    - Default behavior: loading a new skill auto-unmounts the previous
      skill's checks (A).  ``keep_existing=True`` overrides this.
    - Explicit ``unmount()`` is available for manual control (B).
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import ToolRegistry

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Issue #90: skill code integrity verification
# ---------------------------------------------------------------------------

_HASH_DIR = Path.home() / ".loom" / "skill_hashes"


class SkillIntegrityError(RuntimeError):
    """Raised when a skill module's hash doesn't match the recorded value."""


def _compute_file_hash(path: Path) -> str:
    """SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _hash_path_for(skill_dir: Path, module_name: str) -> Path:
    """The legacy record, keyed by the skill directory's name only. Two
    directories with the same name shared it; it is read once, to migrate."""
    return _HASH_DIR / f"{skill_dir.name}_{module_name}.sha256"


def _record_path(module_path: Path) -> Path:
    """The trust record for one module, keyed by its resolved absolute path
    (#700): a same-name copy elsewhere has a record of its own."""
    resolved = module_path.resolve()
    key = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    return _HASH_DIR / f"{resolved.parent.name}_{resolved.stem}.{key}.sha256"


def _trust_record(module_path: Path) -> Path | None:
    """The record that holds *module_path*'s trusted hash: its own, else the
    legacy one, else None (never loaded)."""
    record = _record_path(module_path)
    if record.is_file():
        return record
    legacy = _hash_path_for(module_path.parent, module_path.stem)
    return legacy if legacy.is_file() else None


def _verify_or_record_hash(module_path: Path, skill_dir: Path, module_name: str) -> bool:
    """
    On first load: compute hash and store it.  Return True.
    On subsequent loads: compare.  Return True if match, False if mismatch.
    A matching legacy record is copied to the module's own record.

    Fail-open on I/O errors (log warning, return True) so first-time
    setup is never blocked.
    """
    try:
        current_hash = _compute_file_hash(module_path)
        record = _trust_record(module_path)
        if record is not None and record.read_text().strip() != current_hash:
            return False  # MISMATCH
        own = _record_path(module_path)
        if record != own:  # first load, or a legacy record to migrate
            _HASH_DIR.mkdir(parents=True, exist_ok=True)
            own.write_text(current_hash)
        return True
    except Exception:
        _log.warning("Could not verify hash for %s", module_path)
        return True  # fail-open on I/O errors


@dataclass(frozen=True)
class TrustOffer:
    """A write that would replace a trusted skill check module (#700).

    The platform confirm shows DK ``diff``; if DK allows this one call after
    seeing it, ``retrust_approved_write`` may record ``new_hash``."""
    module: Path     # resolved, exactly <workspace>/skills/<name>/<module>.py
    old_hash: str    # the trusted hash the diff starts from
    new_hash: str    # the approved content's hash
    diff: str


def trust_offer_for_write(workspace: Path, raw: str, content: str) -> TrustOffer | None:
    """A ``TrustOffer`` when writing *content* to *raw* replaces a trusted
    check module, else None. The requested path must be the module itself
    (no symlink alias), sit at ``skills/<name>/<module>.py`` in the
    workspace next to a ``SKILL.md``, and still hold its trusted bytes, so
    the diff starts from what DK trusted before."""
    ws = workspace.resolve()
    literal = Path(os.path.normpath(Path(raw) if Path(raw).is_absolute() else ws / raw))
    if literal.resolve() != literal:
        return None
    try:
        rel = literal.relative_to(ws)
    except ValueError:
        return None
    if len(rel.parts) != 3 or rel.parts[0] != "skills" or rel.suffix != ".py":
        return None
    if not (literal.parent / "SKILL.md").is_file() or not literal.is_file():
        return None
    record = _trust_record(literal)
    if record is None:
        return None
    old_hash = record.read_text().strip()
    if _compute_file_hash(literal) != old_hash:
        return None
    try:
        old = literal.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    diff = "".join(difflib.unified_diff(
        old.splitlines(keepends=True), content.splitlines(keepends=True),
        f"a/{rel.as_posix()}", f"b/{rel.as_posix()}",
    ))
    new_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return TrustOffer(literal, old_hash, new_hash, diff)


def retrust_approved_write(offer: TrustOffer) -> tuple[str, str] | None:
    """Record ``offer.new_hash`` after DK allowed the write having seen the
    diff. Only the caller knows that; this checks the files. The module must
    still be itself (not a symlink), its record still ``offer.old_hash`` and
    its bytes exactly the approved ones. The previous value goes to
    ``_HASH_DIR/backups``. Returns ``(old, new)`` when the record changed."""
    module = offer.module
    if module.resolve() != module or offer.old_hash == offer.new_hash:
        return None
    record = _trust_record(module)
    if record is None or record.read_text().strip() != offer.old_hash:
        return None
    if _compute_file_hash(module) != offer.new_hash:
        return None  # changed after the approved write
    own = _record_path(module)
    backup = _HASH_DIR / "backups" / f"{own.name}.{datetime.now():%Y%m%dT%H%M%S%f}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(offer.old_hash)
    own.write_text(offer.new_hash)
    return offer.old_hash, offer.new_hash


@dataclass
class SkillPreconditionRef:
    """One precondition check declaration from SKILL.md frontmatter."""
    ref: str
    """Dotted reference: ``module.function`` relative to skill directory."""
    applies_to: list[str]
    """Tool names this check should be mounted on."""
    description: str
    """Human-readable description shown in audit trail."""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SkillPreconditionRef:
        """Parse from a frontmatter dict entry."""
        ref = d.get("ref", "")
        applies_to = d.get("applies_to", [])
        if isinstance(applies_to, str):
            applies_to = [applies_to]
        description = d.get("description", ref)
        return cls(ref=ref, applies_to=applies_to, description=description)


# Internal record of a single mounted check — used for unmounting.
@dataclass
class _MountedCheck:
    tool_name: str
    check_fn: Callable
    description: str


class SkillCheckManager:
    """
    Manages the lifecycle of skill-declared precondition checks.

    Instantiated once per session in ``LoomSession.start()``.
    """

    def __init__(self, registry: "ToolRegistry") -> None:
        self._registry = registry
        # skill_name → list of mounted checks
        self._mounted: dict[str, list[_MountedCheck]] = {}
        self._active_skill: str | None = None

    @property
    def active_skill(self) -> str | None:
        """The skill whose checks are currently mounted."""
        return self._active_skill

    def mounted_skills(self) -> list[str]:
        """List of all skills with currently mounted checks."""
        return list(self._mounted.keys())

    # ------------------------------------------------------------------
    # Mount / Unmount
    # ------------------------------------------------------------------

    def activate(self, skill_name: str, keep_existing: bool = False) -> None:
        """
        Declare *skill_name* as the active skill.

        This is a pure lifecycle event — it does NOT mount any checks, but
        it DOES auto-unmount the previous active skill's checks (unless
        ``keep_existing=True``).  Call this on every ``load_skill`` event,
        including skills that declare no precondition checks, so the
        harness invariant holds: ``active_skill`` always reflects the most
        recently loaded skill, and stale checks from a prior skill never
        linger across a skill transition.

        Idempotent: re-activating the currently active skill is a no-op.
        """
        if skill_name == self._active_skill:
            return
        if not keep_existing and self._active_skill is not None:
            self.unmount(self._active_skill)
        self._active_skill = skill_name

    def owner_of(self, check_fn: Callable) -> str | None:
        """Return the skill name that mounted *check_fn*, or None."""
        for skill, checks in self._mounted.items():
            for mc in checks:
                if mc.check_fn is check_fn:
                    return skill
        return None

    def mount(
        self,
        skill_name: str,
        refs: list[SkillPreconditionRef],
        callables: dict[str, Callable],
        keep_existing: bool = False,
    ) -> list[str]:
        """
        Mount precondition checks for *skill_name* onto ToolDefinitions.

        Args:
            skill_name: Name of the skill being loaded.
            refs: Parsed precondition_check_refs from SkillGenome.
            callables: Mapping of ref string → resolved async callable.
            keep_existing: If False (default), auto-unmount the previous
                active skill's checks before mounting.

        Returns:
            List of human-readable descriptions of successfully mounted checks.
        """
        # A: auto-unmount previous skill
        if not keep_existing and self._active_skill and self._active_skill != skill_name:
            self.unmount(self._active_skill)

        # If this skill already has checks mounted, unmount first (re-mount)
        if skill_name in self._mounted:
            self.unmount(skill_name)

        mounted: list[_MountedCheck] = []
        descriptions: list[str] = []

        for ref in refs:
            fn = callables.get(ref.ref)
            if fn is None:
                _log.warning(
                    "Skipping unresolved ref %r for skill %r", ref.ref, skill_name,
                )
                continue

            for tool_name in ref.applies_to:
                tool_def = self._registry.get(tool_name)
                if tool_def is None:
                    _log.warning(
                        "Skill %r: tool %r not found in registry, skipping",
                        skill_name, tool_name,
                    )
                    continue

                tool_def.precondition_checks.append(fn)
                tool_def.preconditions.append(ref.description)
                mounted.append(_MountedCheck(
                    tool_name=tool_name,
                    check_fn=fn,
                    description=ref.description,
                ))
                descriptions.append(f"{tool_name}: {ref.description}")

        self._mounted[skill_name] = mounted
        self._active_skill = skill_name

        _log.info(
            "Mounted %d precondition check(s) for skill %r",
            len(mounted), skill_name,
        )
        return descriptions

    def unmount(self, skill_name: str) -> int:
        """
        Remove all precondition checks previously mounted by *skill_name*.

        Returns the number of checks removed.
        """
        checks = self._mounted.pop(skill_name, [])
        removed = 0

        for mc in checks:
            tool_def = self._registry.get(mc.tool_name)
            if tool_def is None:
                continue
            # Remove by identity — we stored the exact function reference
            try:
                tool_def.precondition_checks.remove(mc.check_fn)
                removed += 1
            except ValueError:
                pass  # already removed (shouldn't happen)
            try:
                tool_def.preconditions.remove(mc.description)
            except ValueError:
                pass

        if self._active_skill == skill_name:
            self._active_skill = None

        _log.info(
            "Unmounted %d precondition check(s) for skill %r",
            removed, skill_name,
        )
        return removed

    def unmount_all(self) -> None:
        """Remove all skill-mounted checks (called on session stop)."""
        for name in list(self._mounted):
            self.unmount(name)

    # ------------------------------------------------------------------
    # Callable resolution
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_callable(skill_dir: Path, ref: str) -> Callable:
        """
        Resolve a dotted ref to an async callable from the skill directory.

        ``ref`` format: ``module.function_name``
        e.g. ``checks.require_not_production`` →
             import ``skill_dir/checks.py``, get ``require_not_production``.

        Raises FileNotFoundError, AttributeError, or TypeError on failure.
        """
        parts = ref.rsplit(".", 1)
        if len(parts) != 2:
            raise ValueError(
                f"Invalid ref format: {ref!r}. Expected 'module.function'."
            )
        module_name, func_name = parts

        module_path = skill_dir / f"{module_name}.py"
        if not module_path.is_file():
            raise FileNotFoundError(
                f"Check module not found: {module_path}"
            )

        # Issue #90: verify code integrity before exec_module
        if not _verify_or_record_hash(module_path, skill_dir, module_name):
            hash_file = _trust_record(module_path)
            raise SkillIntegrityError(
                f"Integrity check failed for {module_path}: file hash does not "
                f"match the recorded hash in {hash_file}. If this change is "
                f"intentional, delete the hash file and reload the skill."
            )

        spec = importlib.util.spec_from_file_location(
            f"loom_skill_check_{skill_dir.name}_{module_name}",
            module_path,
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create module spec for {module_path}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        fn = getattr(module, func_name, None)
        if fn is None:
            raise AttributeError(
                f"Function {func_name!r} not found in {module_path}"
            )
        if not callable(fn):
            raise TypeError(f"{ref!r} resolved to a non-callable: {type(fn)}")

        return fn

    @staticmethod
    def refresh_hash(skill_dir: Path, module_name: str) -> None:
        """Force-recompute and store the hash for a skill module.

        Call this after the user has reviewed and approved a skill change.
        """
        module_path = skill_dir / f"{module_name}.py"
        if not module_path.is_file():
            raise FileNotFoundError(f"Module not found: {module_path}")
        current_hash = _compute_file_hash(module_path)
        _HASH_DIR.mkdir(parents=True, exist_ok=True)
        _record_path(module_path).write_text(current_hash)

    @classmethod
    def resolve_all(
        cls,
        skill_dir: Path,
        refs: list[SkillPreconditionRef],
    ) -> dict[str, Callable]:
        """
        Resolve all refs for a skill.  Returns {ref_string: callable}.

        Raises on the first unresolvable ref.
        """
        result: dict[str, Callable] = {}
        for ref in refs:
            if ref.ref not in result:
                result[ref.ref] = cls.resolve_callable(skill_dir, ref.ref)
        return result
