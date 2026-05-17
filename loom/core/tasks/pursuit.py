"""PursuitStore — filesystem-backed long-lived task artifacts.

Pursuit ≠ memory. Memory (`memory.db`) is the agent's cognition / growth log;
pursuit is a task-axis artifact (issue #375) — user-directed tracking that
lives until the task is done, then gets deleted. Each pursuit is one
markdown file under ``~/.loom/pursuits/<id>.md``.

Shape mirrors ``TaskListManager`` (loom/core/tasks/manager.py) — pure logic
here, tool factories live in ``loom/platform/cli/tools.py``.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class PursuitError(ValueError):
    """Raised for invalid id or missing pursuit."""


def _default_root() -> Path:
    return Path.home() / ".loom" / "pursuits"


class PursuitStore:
    """Read/write/list markdown pursuit files.

    The store owns id ↔ path resolution and atomic write semantics. It does
    NOT interpret pursuit content — the markdown shape is the agent's
    contract with the user, not enforced here.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else _default_root()

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, pursuit_id: str) -> Path:
        if not _ID_RE.match(pursuit_id):
            raise PursuitError(
                f"invalid pursuit id {pursuit_id!r}: "
                "lowercase letters/digits/hyphens only, 1-64 chars, "
                "must start with letter or digit"
            )
        return self._root / f"{pursuit_id}.md"

    def list(self) -> list[str]:
        """Return sorted list of pursuit ids found under root."""
        if not self._root.exists():
            return []
        ids = []
        for entry in self._root.iterdir():
            if entry.is_file() and entry.suffix == ".md":
                stem = entry.stem
                if _ID_RE.match(stem):
                    ids.append(stem)
        ids.sort()
        return ids

    def exists(self, pursuit_id: str) -> bool:
        return self._path(pursuit_id).exists()

    def read(self, pursuit_id: str) -> str:
        path = self._path(pursuit_id)
        if not path.exists():
            raise PursuitError(f"pursuit not found: {pursuit_id}")
        return path.read_text(encoding="utf-8")

    def write(self, pursuit_id: str, content: str) -> Path:
        """Atomic write — tmp file + rename so partial writes never linger."""
        path = self._path(pursuit_id)
        self._root.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{pursuit_id}.", suffix=".md.tmp", dir=str(self._root)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return path

    def delete(self, pursuit_id: str) -> bool:
        """Remove a pursuit. Returns True if a file was removed."""
        path = self._path(pursuit_id)
        if path.exists():
            path.unlink()
            return True
        return False
