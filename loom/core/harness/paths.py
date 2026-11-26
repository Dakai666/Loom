"""Workspace paths as the file tools resolve them, and the content digest
read before write compares (#704 S6).

The guard and the tool must agree on which file a ``path`` argument
names, so both use :func:`resolve_workspace_path`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def resolve_workspace_path(raw: str, workspace: Path) -> Path:
    """Resolve a raw path string strictly relative to workspace.

    - Escaping paths (e.g. ../../Windows) are rerouted back inside workspace
      to safely contain prompt injection and path traversal attempts.
    """
    workspace = workspace.resolve()
    p = Path(raw)
    resolved = (workspace / p).resolve() if not p.is_absolute() else p.resolve()

    try:
        resolved.relative_to(workspace)
        return resolved  # Check passed, securely inside workspace
    except ValueError:
        # Reroute: strip root/drive and forcibly place under workspace
        # e.g. /etc/passwd → workspace/etc/passwd, C:\Windows → workspace\Windows
        parts = resolved.parts[1:]
        return (workspace / Path(*parts)).resolve()


ABSENT = None


def file_digest(path: Path) -> str | None:
    """sha256 of the file's bytes, ``None`` when nothing is there. Anything
    that exists but cannot be read gets a marker no read can match."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return ABSENT
    except OSError as exc:
        if not path.exists() and not path.is_symlink():
            return ABSENT
        return f"unreadable:{type(exc).__name__}"
