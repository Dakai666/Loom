"""
Sandbox Runtime — Issue #29, Quest A Phase 4 (2026-05-18).

Thin integration of `anthropic-experimental/sandbox-runtime` (`srt`) as
optional OS-level confinement for ``run_bash``.  ``srt`` uses native
primitives (macOS ``sandbox-exec`` / Linux ``bubblewrap``) and proxy-based
network filtering — see https://github.com/anthropic-experimental/sandbox-runtime.

**Layering** (see doc/45b, doc/46, doc/55):

    Layer 1 — CommandScanner          tripwire / audit signal
    Layer 2 — Sandbox Runtime (here)  OS-level wall (this module)

Layer 1 stays a defense-in-depth signal; Layer 2 is the actual confinement
for any agent-issued shell.  doc/52 §1.2 picked ``srt`` as the backend.

Opt-in via ``loom.toml``::

    [security.sandbox]
    backend = "srt"                       # "none" | "srt"
    allow_write     = [".", "/private/tmp"]
    deny_read       = ["~/.ssh", "~/.aws", "~/.gnupg", ".env"]
    allowed_domains = ["pypi.org", "files.pythonhosted.org", "api.github.com"]

Default ``backend = "none"`` keeps existing behaviour byte-for-byte.

Installation::

    npm install -g @anthropic-ai/sandbox-runtime
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shlex
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


# srt accepts only one external program: ``srt``.  Pinned here so a future
# rename (e.g. ``sandbox-runtime``) is a single-line change.
_SRT_BINARY = "srt"

# srt's JSON schema rejects payloads missing any top-level section, so the
# renderer always emits every key — even empty arrays.  Keep this in sync
# with srt's `sandbox-schemas.ts`.
_REQUIRED_SECTIONS = ("filesystem", "network", "unixSockets")

_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _as_path_list(key: str, value: Any) -> list[str]:
    """Validate that *value* is ``None`` or a ``list[str]`` (PR #387 review).

    Raises ``ValueError`` on a scalar string (the dangerous case — Python
    would iterate it character-by-character through ``list()``, e.g.
    ``"/tmp"`` → ``["/", "t", "m", "p"]``, and the lone ``"/"`` entry
    becomes a filesystem-wide write allowance under srt's allow-only
    rules) or on a list containing non-string members.
    """
    if value is None:
        return []
    if isinstance(value, str):
        raise ValueError(
            f"[security.sandbox] {key} must be a list of strings, not a "
            f"bare string. Got {value!r}; wrap it in a list: [{value!r}]."
        )
    if not isinstance(value, list):
        raise ValueError(
            f"[security.sandbox] {key} must be a list of strings, got "
            f"{type(value).__name__}: {value!r}."
        )
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(
                f"[security.sandbox] {key}[{i}] must be a string, got "
                f"{type(item).__name__}: {item!r}."
            )
    return list(value)


def _profile_key(profile_name: str, field_name: str) -> str:
    return f"profiles.{profile_name}.{field_name}"


def _validate_profile_name(name: str) -> str:
    if not isinstance(name, str) or not _PROFILE_NAME_RE.match(name):
        raise ValueError(
            f"[security.sandbox] profile name must match "
            f"[A-Za-z0-9][A-Za-z0-9_.-]*, got {name!r}."
        )
    return name


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def extract_command_root(command: str) -> str | None:
    """Return the first executable token after leading KEY=value assignments."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    for token in tokens:
        if _ENV_ASSIGNMENT_RE.match(token):
            continue
        return Path(token).name
    return None


@dataclass(slots=True)
class SandboxProfile:
    """Named per-CLI sandbox expansion, activated only through permission grants."""

    match_commands: list[str] = field(default_factory=list)
    allow_read: list[str] = field(default_factory=list)
    deny_read: list[str] = field(default_factory=list)
    allow_write: list[str] = field(default_factory=list)
    deny_write: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)
    denied_domains: list[str] = field(default_factory=list)
    allowed_sockets: list[str] = field(default_factory=list)
    denied_sockets: list[str] = field(default_factory=list)
    # Issue #402: when True, matched commands skip the srt wrap entirely and
    # run in the parent environment. Required for CLIs whose TLS stack is
    # incompatible with srt's HTTP CONNECT proxy (gh, opencli). The scope
    # grant flow still gates activation — bypass is opt-in per profile,
    # not an unauthorized escape hatch.
    bypass_sandbox: bool = False

    @classmethod
    def from_config(cls, name: str, cfg: dict[str, Any] | None) -> "SandboxProfile":
        if cfg is None:
            cfg = {}
        if not isinstance(cfg, dict):
            raise ValueError(
                f"[security.sandbox] profiles.{name} must be a table, "
                f"got {type(cfg).__name__}: {cfg!r}."
            )
        bypass_raw = cfg.get("bypass_sandbox", False)
        if not isinstance(bypass_raw, bool):
            raise ValueError(
                f"[security.sandbox] profiles.{name}.bypass_sandbox must be "
                f"a boolean, got {type(bypass_raw).__name__}: {bypass_raw!r}."
            )
        return cls(
            match_commands=_as_path_list(
                _profile_key(name, "match_commands"),
                cfg.get("match_commands"),
            ),
            allow_read=_as_path_list(
                _profile_key(name, "allow_read"), cfg.get("allow_read"),
            ),
            deny_read=_as_path_list(
                _profile_key(name, "deny_read"), cfg.get("deny_read"),
            ),
            allow_write=_as_path_list(
                _profile_key(name, "allow_write"), cfg.get("allow_write"),
            ),
            deny_write=_as_path_list(
                _profile_key(name, "deny_write"), cfg.get("deny_write"),
            ),
            allowed_domains=_as_path_list(
                _profile_key(name, "allowed_domains"),
                cfg.get("allowed_domains"),
            ),
            denied_domains=_as_path_list(
                _profile_key(name, "denied_domains"),
                cfg.get("denied_domains"),
            ),
            allowed_sockets=_as_path_list(
                _profile_key(name, "allowed_sockets"),
                cfg.get("allowed_sockets"),
            ),
            denied_sockets=_as_path_list(
                _profile_key(name, "denied_sockets"),
                cfg.get("denied_sockets"),
            ),
            bypass_sandbox=bypass_raw,
        )


@dataclass(slots=True)
class SandboxSettings:
    """Resolved sandbox config for a session.

    ``backend == "none"`` short-circuits ``wrap_command`` — no srt
    invocation, no settings file written.  Other backends may be added
    later; today only ``"srt"`` is recognised.

    Path strings support ``~`` expansion.  A single ``"."`` in any path
    list is anchored at the session workspace when rendered (see
    ``to_srt_json``) so settings stay portable across cwd changes.
    """

    backend: str = "none"
    allow_read: list[str] = field(default_factory=list)
    deny_read: list[str] = field(default_factory=list)
    allow_write: list[str] = field(default_factory=list)
    deny_write: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)
    denied_domains: list[str] = field(default_factory=list)
    allowed_sockets: list[str] = field(default_factory=list)
    denied_sockets: list[str] = field(default_factory=list)
    profiles: dict[str, SandboxProfile] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> "SandboxSettings":
        """Build from the ``[security.sandbox]`` block of loom.toml.

        **Fail-closed validation** (PR #387 review):

        - Unknown ``backend`` values **raise** — silently downgrading to
          ``"none"`` would let a typo like ``backend = "str"`` disable the
          sandbox entirely, contradicting the opt-in security intent.
        - Every list field is validated as ``list[str]``.  Accepting a
          scalar string would silently iterate it character-by-character
          (e.g. ``allow_write = "/tmp"`` → ``["/", "t", "m", "p"]``),
          and the ``"/"`` entry would open writes across the whole
          filesystem under srt's allow-only rules.

        Missing keys yield empty lists (= maximally restrictive for
        allow-only fields like ``allow_write`` / ``allowed_domains``).
        """
        cfg = cfg or {}

        backend_raw = cfg.get("backend")
        if backend_raw is None:
            backend = "none"
        else:
            backend = str(backend_raw).lower()
            if backend not in {"none", "srt"}:
                raise ValueError(
                    f"[security.sandbox] backend = {backend_raw!r} is not "
                    f"recognized. Valid options: 'none', 'srt'."
                )

        raw_profiles = cfg.get("profiles", {}) or {}
        if not isinstance(raw_profiles, dict):
            raise ValueError(
                f"[security.sandbox] profiles must be a table, got "
                f"{type(raw_profiles).__name__}: {raw_profiles!r}."
            )
        profiles = {
            _validate_profile_name(name): SandboxProfile.from_config(name, value)
            for name, value in raw_profiles.items()
        }
        command_to_profile: dict[str, str] = {}
        for profile_name, profile in profiles.items():
            for command in profile.match_commands:
                previous = command_to_profile.setdefault(command, profile_name)
                if previous != profile_name:
                    raise ValueError(
                        "[security.sandbox] profiles match_commands must be "
                        f"unique; command {command!r} is used by both "
                        f"{previous!r} and {profile_name!r}."
                    )

        return cls(
            backend=backend,
            allow_read=_as_path_list("allow_read", cfg.get("allow_read")),
            deny_read=_as_path_list("deny_read", cfg.get("deny_read")),
            allow_write=_as_path_list("allow_write", cfg.get("allow_write")),
            deny_write=_as_path_list("deny_write", cfg.get("deny_write")),
            allowed_domains=_as_path_list(
                "allowed_domains", cfg.get("allowed_domains")
            ),
            denied_domains=_as_path_list(
                "denied_domains", cfg.get("denied_domains")
            ),
            allowed_sockets=_as_path_list(
                "allowed_sockets", cfg.get("allowed_sockets")
            ),
            denied_sockets=_as_path_list(
                "denied_sockets", cfg.get("denied_sockets")
            ),
            profiles=profiles,
        )

    def select_profile_for_command(
        self, command: str, *, explicit: str | None = None,
    ) -> str | None:
        if explicit:
            if explicit not in self.profiles:
                raise ValueError(f"Unknown sandbox profile {explicit!r}.")
            return explicit

        root = extract_command_root(command)
        if not root:
            return None
        matches = [
            name for name, profile in self.profiles.items()
            if root in profile.match_commands
        ]
        if len(matches) > 1:
            raise ValueError(
                f"Command {root!r} matches multiple sandbox profiles: "
                + ", ".join(sorted(matches))
            )
        return matches[0] if matches else None

    def merged_with_profile(self, profile_name: str) -> "SandboxSettings":
        if profile_name not in self.profiles:
            raise ValueError(f"Unknown sandbox profile {profile_name!r}.")
        p = self.profiles[profile_name]
        return SandboxSettings(
            backend=self.backend,
            allow_read=_dedupe(self.allow_read + p.allow_read),
            deny_read=_dedupe(self.deny_read + p.deny_read),
            allow_write=_dedupe(self.allow_write + p.allow_write),
            deny_write=_dedupe(self.deny_write + p.deny_write),
            allowed_domains=_dedupe(self.allowed_domains + p.allowed_domains),
            denied_domains=_dedupe(self.denied_domains + p.denied_domains),
            allowed_sockets=_dedupe(self.allowed_sockets + p.allowed_sockets),
            denied_sockets=_dedupe(self.denied_sockets + p.denied_sockets),
            profiles={},
        )

    def to_srt_json(self, workspace: Path | None = None) -> dict[str, Any]:
        """Render to srt's JSON config shape with all required sections."""

        def _expand(paths: list[str]) -> list[str]:
            out: list[str] = []
            for raw in paths:
                p = os.path.expanduser(raw)
                if workspace is not None and p == ".":
                    p = str(workspace)
                out.append(p)
            return out

        return {
            "filesystem": {
                "allowRead": _expand(self.allow_read),
                "denyRead": _expand(self.deny_read),
                "allowWrite": _expand(self.allow_write),
                "denyWrite": _expand(self.deny_write),
            },
            "network": {
                "allowedDomains": list(self.allowed_domains),
                "deniedDomains": list(self.denied_domains),
            },
            "unixSockets": {
                "allowedPaths": list(self.allowed_sockets),
                "deniedPaths": list(self.denied_sockets),
            },
        }


def srt_available() -> bool:
    """True iff the ``srt`` CLI is on PATH."""
    return shutil.which(_SRT_BINARY) is not None


def srt_install_hint() -> str:
    return (
        "srt binary not found on PATH. Install with:\n"
        "    npm install -g @anthropic-ai/sandbox-runtime\n"
        "Or set [security.sandbox] backend = \"none\" in loom.toml."
    )


def write_settings_file(
    settings: SandboxSettings, workspace: Path, *, dir: Path | None = None
) -> Path:
    """Render *settings* to a JSON file and return its path.

    The path is stable per rendered payload so base and profile-merged
    settings cannot overwrite each other while repeated equivalent wraps
    reuse one file. Caller is not required to clean up; the OS reaps
    tmpfiles on the usual schedule.
    """
    payload = settings.to_srt_json(workspace=workspace)
    base = Path(dir) if dir is not None else Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    h = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:12]
    path = base / f"loom-srt-{h}.json"
    path.write_text(rendered)
    return path


def wrap_command(command: str, settings_path: Path) -> str:
    """Prepend ``srt -s <settings> -c <command>`` to *command*.

    ``-c`` takes the command as a single string (sh-c semantics) so no
    extra escaping is needed beyond shell-quoting the two arguments.
    The returned string is meant to be passed to a shell — typically
    ``asyncio.create_subprocess_shell``.
    """
    return f"{_SRT_BINARY} -s {shlex.quote(str(settings_path))} -c {shlex.quote(command)}"
