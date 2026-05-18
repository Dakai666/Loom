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
import logging
import os
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

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> "SandboxSettings":
        """Build from the ``[security.sandbox]`` block of loom.toml.

        Unknown ``backend`` values downgrade to ``"none"`` with a warning;
        missing keys yield empty lists (= maximally restrictive for
        allow-only fields like ``allow_write`` / ``allowed_domains``).
        """
        cfg = cfg or {}
        backend = str(cfg.get("backend", "none")).lower()
        if backend not in {"none", "srt"}:
            _log.warning(
                "Unknown sandbox backend %r; falling back to 'none'.", backend
            )
            backend = "none"
        return cls(
            backend=backend,
            allow_read=list(cfg.get("allow_read", []) or []),
            deny_read=list(cfg.get("deny_read", []) or []),
            allow_write=list(cfg.get("allow_write", []) or []),
            deny_write=list(cfg.get("deny_write", []) or []),
            allowed_domains=list(cfg.get("allowed_domains", []) or []),
            denied_domains=list(cfg.get("denied_domains", []) or []),
            allowed_sockets=list(cfg.get("allowed_sockets", []) or []),
            denied_sockets=list(cfg.get("denied_sockets", []) or []),
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

    The path is stable per (workspace, pid) so repeated wraps within a
    session reuse the same file — avoids littering ``$TMPDIR`` with one
    file per shell call.  Caller is not required to clean up; the OS
    reaps tmpfiles on the usual schedule.
    """
    payload = settings.to_srt_json(workspace=workspace)
    base = Path(dir) if dir is not None else Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    h = abs(hash((str(workspace), os.getpid()))) % 10**8
    path = base / f"loom-srt-{h}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def wrap_command(command: str, settings_path: Path) -> str:
    """Prepend ``srt -s <settings> -c <command>`` to *command*.

    ``-c`` takes the command as a single string (sh-c semantics) so no
    extra escaping is needed beyond shell-quoting the two arguments.
    The returned string is meant to be passed to a shell — typically
    ``asyncio.create_subprocess_shell``.
    """
    return f"{_SRT_BINARY} -s {shlex.quote(str(settings_path))} -c {shlex.quote(command)}"
