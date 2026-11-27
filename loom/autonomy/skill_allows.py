"""A skill's ``allows``: the one boundary of the activity it describes (#708 S1).

DK's plan (2026-11-27): a skill's SKILL.md frontmatter may carry ``allows``,
listing only that skill's own things::

    allows:
      exec:                     # exact command prefixes (argv shapes)
        - python3 skills/pet-cat/pet.py status
        - python3 skills/pet-cat/pet.py feed
      write:                    # workspace path prefixes
        - skills/pet-cat/notes
      tools: [memorize]         # extra tools the activity declares

General tools (memorize, search, read) declare their own effect and follow
the permission_mode everywhere; ``allows`` is for what only this skill does.
The format is deliberately minimal: exact prefixes and names, no wildcards,
no conditions. Each entry translates one to one into the forms the harness
already enforces, through :func:`parse_permission_fields`:

- ``exec``  → ``{resource="exec", action="execute", selector=<shape>}``
- ``write`` → ``{resource="path", action="write", selector=<prefix>}``
- ``tools`` → ``allowed_tools``

``allows`` sits beside ``precondition_checks`` in the guarded part of the
frontmatter, so changing it is a ``skill_guard`` change that DK approves
(#704 S3, ``approval._skill_guard``). No new approval mechanism.

Who reads it: only a furniture wake in S1 (``circadian/room.py``), at wake
start, so the boundary is on the chime before the turn reads the skill.
Loading a skill never turns ``allows`` into grants: an attended turn stays
under its permission_mode. S2 points rhythm anchors and schedules at the
same reader.

Unlike the tolerant autonomy loaders, this reader is strict: a missing skill,
a malformed ``allows`` or a script that does not exist raises
:class:`SkillAllowsError`, so the caller can reject the furniture loudly
instead of a wake quietly skipping its work (喵吉 since 11-22).
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from loom.autonomy.permission_fields import parse_permission_fields
from loom.core.harness.scope import simple_argv

ALLOWS_KEYS = ("exec", "write", "tools")
_WILDCARD_CHARS = frozenset("*?[]{}~$")


class SkillAllowsError(ValueError):
    """A skill's ``allows`` cannot be used as a boundary (message says why)."""


@dataclass(frozen=True)
class SkillAllows:
    exec: tuple[str, ...] = ()
    write: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()


def _frontmatter(text: str, where: str) -> dict[str, Any]:
    import yaml

    if not text.startswith("---"):
        raise SkillAllowsError(f"{where} has no frontmatter")
    end = text.find("\n---", 3)
    if end < 0:
        raise SkillAllowsError(f"{where} frontmatter is not closed")
    try:
        front = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError as exc:
        raise SkillAllowsError(f"{where} frontmatter does not parse: {exc}") from None
    if not isinstance(front, dict):
        raise SkillAllowsError(f"{where} frontmatter is not a table")
    return front


def _str_list(raw: Any, key: str, where: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(v, str) and v.strip() for v in raw):
        raise SkillAllowsError(f"{where} allows.{key} must be a list of non-empty strings")
    return [v.strip() for v in raw]


def _plain_prefix(path: str) -> PurePosixPath | None:
    """*path* as a normalised workspace-relative prefix, or None when it is
    absolute, escapes the workspace, names the workspace itself or holds a
    wildcard."""
    if not path or any(c in _WILDCARD_CHARS for c in path) or path.startswith("/"):
        return None
    norm = posixpath.normpath(path)
    if norm in (".", "") or norm == ".." or norm.startswith("../"):
        return None
    return PurePosixPath(norm)


def _check_exec(shape: str, skill_dir: PurePosixPath, workspace: Path, where: str) -> None:
    if any(c in _WILDCARD_CHARS for c in shape):
        raise SkillAllowsError(f"{where} allows.exec {shape!r}: exact prefixes only, no wildcards")
    argv = simple_argv(shape)
    if argv is None:
        raise SkillAllowsError(f"{where} allows.exec {shape!r} is not one plain command")
    scripts = [a for a in argv if "/" in a]
    if not scripts:
        raise SkillAllowsError(
            f"{where} allows.exec {shape!r} names no script of this skill ({skill_dir}/…)")
    for token in scripts:
        rel = _plain_prefix(token)
        if rel is None or not rel.is_relative_to(skill_dir):
            raise SkillAllowsError(
                f"{where} allows.exec {shape!r}: {token} is not this skill's own script "
                f"(must live under {skill_dir}/)")
        if not (workspace / rel).is_file():
            raise SkillAllowsError(f"{where} allows.exec {shape!r}: {token} does not exist")


def read_skill_allows(name: str, workspace: Path) -> SkillAllows:
    """The ``allows`` of workspace skill *name* (``skills/<name>/SKILL.md``).

    Raises :class:`SkillAllowsError` when the skill is missing, declares no
    ``allows`` (write ``allows: {}`` to say "nothing"), or declares one that
    is malformed or names a script that does not exist.
    """
    if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
        raise SkillAllowsError(f"skill name {name!r} must be one directory under skills/")
    skill_dir = PurePosixPath("skills") / name
    skill_md = workspace / skill_dir / "SKILL.md"
    where = f"skill {name!r}"
    try:
        text = skill_md.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SkillAllowsError(f"{where} not found ({skill_dir}/SKILL.md)") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillAllowsError(f"{where} unreadable ({skill_dir}/SKILL.md): {exc}") from None
    front = _frontmatter(text, where)
    if "allows" not in front:
        raise SkillAllowsError(
            f"{where} declares no allows (write `allows: {{}}` if the activity needs nothing)")
    raw = front["allows"]
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SkillAllowsError(f"{where} allows must be a table of {', '.join(ALLOWS_KEYS)}")
    unknown = sorted(str(k) for k in raw if k not in ALLOWS_KEYS)
    if unknown:
        raise SkillAllowsError(
            f"{where} allows has unknown key(s) {', '.join(unknown)}; only {', '.join(ALLOWS_KEYS)}")

    exec_ = _str_list(raw.get("exec"), "exec", where)
    for shape in exec_:
        _check_exec(shape, skill_dir, workspace, where)
    write = _str_list(raw.get("write"), "write", where)
    for path in write:
        if _plain_prefix(path) is None:
            raise SkillAllowsError(
                f"{where} allows.write {path!r} must be a plain path inside the workspace")
    tools = _str_list(raw.get("tools"), "tools", where)
    for tool in tools:
        if any(c in _WILDCARD_CHARS for c in tool) or any(c.isspace() for c in tool):
            raise SkillAllowsError(f"{where} allows.tools {tool!r} must be an exact tool name")
    return SkillAllows(exec=tuple(exec_), write=tuple(write), tools=tuple(tools))


def allows_permission_fields(allows: SkillAllows, source_name: str) -> dict[str, Any]:
    """*allows* in the existing allowed_tools / scope_grants forms."""
    grants = [{"resource": "exec", "action": "execute", "selector": s} for s in allows.exec]
    grants += [{"resource": "path", "action": "write", "selector": p} for p in allows.write]
    return parse_permission_fields(
        {"allowed_tools": list(allows.tools), "scope_grants": grants}, source_name)


def widening(allows: SkillAllows, tools: list[str] | tuple[str, ...],
             grants: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> list[str]:
    """What in *tools* / *grants* lies outside *allows* (empty: a narrowing).

    A tool must be one ``allows.tools`` names; an exec grant's shape must
    start with an ``allows.exec`` shape; a path write grant must sit under an
    ``allows.write`` prefix. Constraints only ever narrow a grant, so they
    are kept as written. Anything else widens.
    """
    out = [f"tool {t}" for t in tools if t not in allows.tools]
    shapes = [simple_argv(s) for s in allows.exec]
    prefixes = [_plain_prefix(p) for p in allows.write]
    for g in grants:
        res, act, sel = g.get("resource"), g.get("action"), g.get("selector", "*")
        if res == "exec" and act == "execute" and isinstance(sel, str):
            argv = None if any(c in _WILDCARD_CHARS for c in sel) else simple_argv(sel)
            if argv is not None and any(
                    s is not None and argv[:len(s)] == s for s in shapes):
                continue
        elif res == "path" and act == "write" and isinstance(sel, str):
            rel = _plain_prefix(sel)
            if rel is not None and any(p is not None and rel.is_relative_to(p) for p in prefixes):
                continue
        out.append(f"{res}:{act} {sel}")
    return out
