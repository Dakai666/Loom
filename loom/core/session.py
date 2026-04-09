"""Stable core entrypoint for the live Loom session runtime.

The concrete ``LoomSession`` implementation currently lives in
``loom.platform.cli.main`` because the CLI, TUI, and Discord frontends
share the same session engine.  This module provides a core-level import
path so framework consumers do not need to depend on a platform module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.platform.cli.main import LoomSession as LoomSession

__all__ = ["LoomSession"]


def __getattr__(name: str) -> Any:
    if name == "LoomSession":
        from loom.platform.cli.main import LoomSession

        return LoomSession
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
