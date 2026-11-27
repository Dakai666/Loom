"""A skill's ``allows``: the one boundary of the activity it describes (#708 S1).

DK's plan (2026-11-27): a skill's SKILL.md frontmatter may carry ``allows``,
listing only that skill's own things::

    allows:
      exec:                     # exact command prefixes (argv shapes)
        - python3 skills/pet-cat/pet.py status
        - python3 skills/pet-cat/pet.py feed
      write:                    # workspace directory prefixes
        - skills/pet-cat/notes
      tools: [memorize]         # extra tools the activity declares

General tools (memorize, search, read) declare their own effect and follow
the permission_mode everywhere; ``allows`` is for what only this skill does.
The format is deliberately minimal: exact prefixes and names, no wildcards,
no conditions. Each entry translates one to one into the forms the harness
already enforces, through :func:`parse_permission_fields`:

- ``exec``  → ``{resource="exec", action="execute", selector=<shape>,
  constraints={absolute_paths="deny"}}``
- ``write`` → ``{resource="path", action="write", selector=<prefix>}``
- ``tools`` → ``allowed_tools``

What a shape, a prefix or a tool may be (#714 review, 克萊兒 · Antigravity):

- **exec** runs one of this skill's own scripts and nothing else: either
  ``python3`` / ``python`` followed directly by the script (no interpreter
  flags such as ``-c`` / ``-m`` before it), or the script itself. Every
  script path resolves (symlinks followed) to an existing file inside the
  skill's own directory, which itself must not be a symlink.
- **write** is a directory prefix (``write_file`` grants match the file's
  parent directory). It may not cover a control or policy file: a skill's
  directory or its SKILL.md / ``*.py`` guard files, ``autonomy/`` config,
  ``loom.toml``, hidden or credential paths, or any existing file under it
  that the hard guardrail protects. It may not pass through a symlink.
  At run time every granted write still meets ``hard_boundary``.
- **tools** never names a scoped tool (``write_file``, ``run_bash``,
  ``spawn_agent``): those go through ``write`` / ``exec``, where they are
  bounded.

``allows`` sits beside ``precondition_checks`` in the guarded part of the
frontmatter, so changing it is a ``skill_guard`` change that DK approves
(#704 S3, ``approval._skill_guard``). No new approval mechanism.

Who reads it: furniture wakes (S1, ``circadian/room.py``), rhythm anchors
and schedules (S2), at config load, before the turn reads the skill.
Loading a skill never turns ``allows`` into grants: an attended turn stays
under its permission_mode. S2 resolves multiple references as a union.
Only workspace skills (``<workspace>/skills/<name>``) are read:
their SKILL.md is the one the skill guard covers.

Unlike the tolerant autonomy loaders, this reader is strict: a missing skill,
a malformed ``allows`` or a script that does not exist raises
:class:`SkillAllowsError`, so the caller can reject the furniture loudly
instead of a wake quietly skipping its work (喵吉 since 11-22).

Known limit: exec grants match argv prefixes, not OS isolation. The checks
run every time the room reloads (each tick), but a script the skill owns can
still do anything once it runs; ``pet.py status`` writes state.
"""

from __future__ import annotations

import logging
import os
import posixpath
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from loom.autonomy.permission_fields import parse_permission_fields
from loom.core.harness.scope import simple_argv

ALLOWS_KEYS = ("exec", "write", "tools")
_WILDCARD_CHARS = frozenset("*?[]{}~$")
INTERPRETERS = frozenset({"python3", "python"})
# Tools whose reach a scope bounds: declaring one by name would hand the
# activity an unscoped version of what exec / write bound.
SCOPED_TOOLS = frozenset({"write_file", "run_bash", "spawn_agent"})
# Policy files a write prefix may never cover, whether or not they exist yet.
_PROTECTED_FILES = (
    "loom.toml", ".env", "autonomy/schedules.toml",
    "autonomy/circadian/rhythm.toml", "autonomy/circadian/room.toml",
)
_EXEC_CONSTRAINTS = {"absolute_paths": "deny"}
_SCAN_LIMIT = 2000
logger = logging.getLogger(__name__)


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


def _real_skill_dir(skill_dir: PurePosixPath, workspace: Path, where: str) -> Path:
    """The skill's directory, which must be a real directory (not a
    symlink) inside the workspace."""
    ws_real = workspace.resolve()
    lexical = ws_real / skill_dir
    try:
        real = lexical.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        raise SkillAllowsError(f"{where} not found ({skill_dir}/SKILL.md)") from None
    except (OSError, RuntimeError) as exc:
        raise SkillAllowsError(f"{where} unreadable ({skill_dir}): {exc}") from None
    if real != lexical or not real.is_dir():
        raise SkillAllowsError(
            f"{where}: {skill_dir} must be a real directory inside the workspace, not a symlink")
    return real


def _own_file(token: str, skill_dir: PurePosixPath, skill_real: Path, workspace: Path,
              shape: str, where: str) -> None:
    """*token* names an existing file of this skill, after symlinks."""
    rel = _plain_prefix(token)
    if rel is None or not rel.is_relative_to(skill_dir):
        raise SkillAllowsError(
            f"{where} allows.exec {shape!r}: {token} is not this skill's own script "
            f"(must live under {skill_dir}/)")
    try:
        real = (workspace.resolve() / rel).resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        raise SkillAllowsError(f"{where} allows.exec {shape!r}: {token} does not exist") from None
    except (OSError, RuntimeError) as exc:
        raise SkillAllowsError(f"{where} allows.exec {shape!r}: {token} unreadable: {exc}") from None
    if not real.is_relative_to(skill_real) or not real.is_file():
        raise SkillAllowsError(
            f"{where} allows.exec {shape!r}: {token} resolves outside {skill_dir}/ "
            "(a symlink may not lead out of the skill)")


def _check_exec(shape: str, skill_dir: PurePosixPath, skill_real: Path, workspace: Path,
                where: str) -> None:
    if any(c in _WILDCARD_CHARS for c in shape):
        raise SkillAllowsError(f"{where} allows.exec {shape!r}: exact prefixes only, no wildcards")
    argv = simple_argv(shape)
    if argv is None:
        raise SkillAllowsError(f"{where} allows.exec {shape!r} is not one plain command")
    # The program is the skill's script, or a Python interpreter whose very
    # next word is the script: no -c / -m / -u before it (#714 B3).
    if argv[0] in INTERPRETERS:
        if len(argv) < 2 or argv[1].startswith("-") or "/" not in argv[1]:
            raise SkillAllowsError(
                f"{where} allows.exec {shape!r}: {argv[0]} must be followed directly by "
                f"this skill's script ({skill_dir}/…), with no interpreter options")
        script_at = 1
    elif "/" in argv[0]:
        script_at = 0
    else:
        raise SkillAllowsError(
            f"{where} allows.exec {shape!r}: the program must be this skill's script or "
            f"{' / '.join(sorted(INTERPRETERS))} running it")
    for token in argv[script_at:]:
        if token == argv[script_at] or "/" in token:
            _own_file(token, skill_dir, skill_real, workspace, shape, where)


def _check_write(path: str, workspace: Path, where: str) -> None:
    """A write prefix stays off control and policy files (#714 B4)."""
    from loom.core.harness.approval import protected_write_target

    rel = _plain_prefix(path)
    if rel is None:
        raise SkillAllowsError(
            f"{where} allows.write {path!r} must be a plain path inside the workspace")
    low = PurePosixPath(*(part.lower() for part in rel.parts))

    def refuse(why: str) -> SkillAllowsError:
        return SkillAllowsError(f"{where} allows.write {path!r} {why}")

    if any(part.startswith(".") for part in low.parts):
        raise refuse("is a hidden path")
    if low.parts[0] == "skills" and (
            len(low.parts) < 3
            or (len(low.parts) == 3 and (low.name == "skill.md" or low.suffix == ".py"))):
        raise refuse("covers a skill's directory or guard files (SKILL.md, *.py); "
                     "name a subdirectory such as skills/<name>/notes")
    for protected in _PROTECTED_FILES:
        if PurePosixPath(protected).is_relative_to(low):
            raise refuse(f"covers {protected}")
    if reason := protected_write_target(rel / "_", workspace):
        if reason not in ("policy_file", "control_file", "skill_guard"):
            raise refuse(f"is protected ({reason})")
    # No part of it may be a symlink: the grant would reach where it leads.
    ws_real = workspace.resolve()
    here = ws_real
    for part in rel.parts:
        here = here / part
        if here.is_symlink():
            raise refuse(f"passes through a symlink ({here.relative_to(ws_real)})")
        if not here.exists():
            break
    target = ws_real / rel
    if target.is_file():
        raise refuse("is a file; write prefixes are directories")
    if not target.is_dir():
        return
    seen = 0
    for root, dirs, files in os.walk(target, followlinks=False):
        for name in dirs + files:
            seen += 1
            if seen > _SCAN_LIMIT:
                raise refuse(f"covers more than {_SCAN_LIMIT} entries; name a narrower directory")
            full = Path(root) / name
            sub = PurePosixPath(full.relative_to(ws_real).as_posix())
            if full.is_symlink():
                raise refuse(f"holds a symlink ({sub})")
            if name in files and (reason := protected_write_target(sub, workspace)):
                raise refuse(f"covers {sub} ({reason})")


def read_skill_allows(name: str, workspace: Path) -> SkillAllows:
    """The ``allows`` of workspace skill *name* (``skills/<name>/SKILL.md``).

    Raises :class:`SkillAllowsError` when the skill is missing, declares no
    ``allows`` (write ``allows: {}`` to say "nothing"), or declares one that
    is malformed or crosses the rules in the module docstring.
    """
    if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
        raise SkillAllowsError(f"skill name {name!r} must be one directory under skills/")
    skill_dir = PurePosixPath("skills") / name
    where = f"skill {name!r}"
    skill_real = _real_skill_dir(skill_dir, workspace, where)
    skill_md = skill_real / "SKILL.md"
    if skill_md.is_symlink():
        raise SkillAllowsError(f"{where}: {skill_dir}/SKILL.md must not be a symlink")
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
        # `allows:` with no value is a half-written table, not "nothing" (#714 N2).
        raise SkillAllowsError(
            f"{where} allows has no value (write `allows: {{}}` if the activity needs nothing)")
    if not isinstance(raw, dict):
        raise SkillAllowsError(f"{where} allows must be a table of {', '.join(ALLOWS_KEYS)}")
    unknown = sorted(str(k) for k in raw if k not in ALLOWS_KEYS)
    if unknown:
        raise SkillAllowsError(
            f"{where} allows has unknown key(s) {', '.join(unknown)}; only {', '.join(ALLOWS_KEYS)}")

    exec_ = _str_list(raw.get("exec"), "exec", where)
    for shape in exec_:
        _check_exec(shape, skill_dir, skill_real, workspace, where)
    write = _str_list(raw.get("write"), "write", where)
    for path in write:
        _check_write(path, workspace, where)
    tools = _str_list(raw.get("tools"), "tools", where)
    for tool in tools:
        if any(c in _WILDCARD_CHARS for c in tool) or any(c.isspace() for c in tool):
            raise SkillAllowsError(f"{where} allows.tools {tool!r} must be an exact tool name")
        if tool in SCOPED_TOOLS:
            raise SkillAllowsError(
                f"{where} allows.tools {tool!r} is a scoped tool; list its scripts in "
                "allows.exec or its directories in allows.write instead")
    return SkillAllows(exec=tuple(exec_), write=tuple(write), tools=tuple(tools))


def harden(grants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*grants* with every exec grant denying absolute paths (#714 N3), so a
    shape never covers ``pet.py status /etc/passwd``."""
    out = []
    for g in grants:
        if g.get("resource") == "exec":
            g = {**g, "constraints": {**(g.get("constraints") or {}), **_EXEC_CONSTRAINTS}}
        out.append(g)
    return out


def allows_permission_fields(allows: SkillAllows, source_name: str) -> dict[str, Any]:
    """*allows* in the existing allowed_tools / scope_grants forms."""
    grants = [{"resource": "exec", "action": "execute", "selector": s} for s in allows.exec]
    grants += [{"resource": "path", "action": "write", "selector": p} for p in allows.write]
    fields = parse_permission_fields(
        {"allowed_tools": list(allows.tools), "scope_grants": grants}, source_name)
    fields["scope_grants"] = harden(fields["scope_grants"])
    return fields


def widening(allows: SkillAllows, tools: list[str] | tuple[str, ...],
             grants: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> list[str]:
    """What in *tools* / *grants* lies outside *allows* (empty: a narrowing).

    A tool must be one ``allows.tools`` names; an exec grant's shape must
    start with an ``allows.exec`` shape; a path write grant must sit under an
    ``allows.write`` prefix. Constraints only ever narrow a grant, so they
    are kept as written (and exec grants are hardened by the caller).
    Anything else widens.
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


def activity_permission_fields(
    entry: dict[str, Any], source_name: str, workspace: Path,
) -> dict[str, Any]:
    """Resolve an anchor/schedule ceiling once, before its turn (#708 S2).

    Multiple skills form a union. Explicit scope_grants REPLACE the derived
    grants, and may only narrow them (including existing max_calls).
    Never append an unlimited derived grant alongside its limited override.
    allowed_tools remains an ordinary tool declaration, not a skill scope
    grant: the existing resolver/mode/hard-guard path decides its calls.
    S1 furniture keeps its own stricter subset contract, unchanged.
    """
    own = parse_permission_fields(entry, source_name)
    if "skills" not in entry:
        if "scope_grants" in entry:
            # Staged compatibility: unrelated legacy activities cannot all
            # migrate to S1 allows (e.g. arbitrary CLI / path:read). Keep
            # their existing contract until explicitly migrated, loudly.
            logger.warning(
                "[autonomy] %s uses legacy scope_grants without skills; retained for "
                "compatibility, migrate activity scopes to SKILL.md allows and skills",
                source_name)
        return own
    names = entry["skills"]
    if (not isinstance(names, list) or not names
            or not all(isinstance(n, str) and n for n in names)):
        raise SkillAllowsError("skills must be a non-empty list of skill names")
    boundaries = [read_skill_allows(n, workspace) for n in dict.fromkeys(names)]
    union = SkillAllows(**{
        key: tuple(dict.fromkeys(v for a in boundaries for v in getattr(a, key)))
        for key in ALLOWS_KEYS
    })
    derived = allows_permission_fields(union, source_name)
    tools, grants = derived["allowed_tools"], derived["scope_grants"]
    if "allowed_tools" in entry:
        raw = entry["allowed_tools"]
        if not isinstance(raw, list) or not all(isinstance(t, str) and t.strip() for t in raw):
            raise SkillAllowsError("allowed_tools must be a list of non-empty tool names")
        tools = [t.strip() for t in raw]
    if "scope_grants" in entry:
        raw = entry["scope_grants"]
        if not isinstance(raw, list) or len(raw) != len(own["scope_grants"]):
            raise SkillAllowsError("scope_grants must be a list of complete grants")
        grants = own["scope_grants"]
        for grant in grants:
            constraints = grant.get("constraints", {})
            if not isinstance(constraints, dict):
                raise SkillAllowsError("grant constraints must be a table")
            for key, value in constraints.items():
                if key == "max_calls":
                    valid = isinstance(value, int) and not isinstance(value, bool) and value >= 0
                elif key == "exact_argv":
                    valid = isinstance(value, bool)
                elif key == "absolute_paths":
                    valid = value == "deny"
                else:
                    valid = False
                if not valid:
                    raise SkillAllowsError(f"unsupported or invalid narrowing constraint {key}={value!r}")
    # Tool names declare usage, not activity scopes. In particular EXEC
    # names never authorize a command; middleware._declared enforces that.
    # Only scopes must be subsets here. Do not relax S1's widening helper.
    if outside := widening(union, [], grants):
        raise SkillAllowsError("widens skill allows: " + ", ".join(outside))
    if "write_file" in tools and not any(
            g.get("resource") == "path" and g.get("action") == "write" for g in grants):
        raise SkillAllowsError("write_file declaration requires an effective path:write grant")
    return {"trust_level": own["trust_level"], "allowed_tools": list(tools),
            "scope_grants": harden(grants)}


def activity_workspace(path: Path, default_path: Path, workspace: Path | None) -> Path:
    """Default-layout configs resolve skills from their workspace, not cwd.

    An explicitly located standalone config uses its parent; callers with a
    custom layout may pass workspace explicitly.
    """
    if workspace is not None:
        return workspace
    resolved = path.resolve()
    if resolved.parts[-len(default_path.parts):] == default_path.parts:
        return resolved.parents[len(default_path.parts) - 1]
    return resolved.parent
