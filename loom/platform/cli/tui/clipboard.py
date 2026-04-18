"""
Native clipboard helper.

Textual's App.copy_to_clipboard uses OSC 52, which macOS Terminal.app ignores.
This helper shells out to the platform-native tool first (pbcopy / xclip / xsel
/ clip), then falls back to OSC 52 via the passed-in Textual app so remote
SSH sessions still work.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Protocol


class _AppWithOSC52(Protocol):
    def copy_to_clipboard(self, text: str) -> None: ...


def _native_copy(text: str) -> bool:
    """Try the OS-native clipboard tool. Return True on success."""
    if sys.platform == "darwin":
        cmd = ["pbcopy"]
    elif sys.platform == "win32":
        cmd = ["clip"]
    else:
        if shutil.which("wl-copy"):
            cmd = ["wl-copy"]
        elif shutil.which("xclip"):
            cmd = ["xclip", "-selection", "clipboard"]
        elif shutil.which("xsel"):
            cmd = ["xsel", "--clipboard", "--input"]
        else:
            return False
    try:
        proc = subprocess.run(
            cmd, input=text.encode("utf-8"), check=True, timeout=2
        )
        return proc.returncode == 0
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return False


def copy_text(app: _AppWithOSC52, text: str) -> None:
    """Copy text to the system clipboard; fall back to OSC 52 for remote ttys."""
    if not _native_copy(text):
        app.copy_to_clipboard(text)
