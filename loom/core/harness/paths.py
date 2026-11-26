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
NOT_REGULAR = "not-regular:"
"""Prefix of the digest of something that exists but is not a regular file
(a directory, a FIFO, a device). Read before write never overwrites it."""

_CHUNK = 1 << 20


def file_digest(path: Path) -> str | None:
    """sha256 of the file's bytes, ``None`` when nothing is there.

    Opened non-blocking and checked with ``fstat`` on the open descriptor
    before any read, so a FIFO or device never blocks the event loop
    (#711 review); only a regular file is hashed, in 1 MB chunks. Anything
    else gets a ``NOT_REGULAR`` marker, and a file that exists but cannot
    be read gets a marker no read can match."""
    import os
    import stat
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return ABSENT
    except IsADirectoryError:
        return f"{NOT_REGULAR}dir"
    except OSError as exc:
        if not os.path.lexists(path):
            return ABSENT
        return f"unreadable:{type(exc).__name__}"
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            return f"{NOT_REGULAR}{stat.S_IFMT(mode):o}"
        h = hashlib.sha256()
        while chunk := os.read(fd, _CHUNK):
            h.update(chunk)
        return h.hexdigest()
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"
    finally:
        os.close(fd)
