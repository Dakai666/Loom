"""Public loader for ``loom.toml`` runtime configuration.

Single source of truth for the file-system search + parse logic. Other
layers (session, autonomy, cognition) import ``load_loom_config`` instead
of reaching into private session-module helpers — see #306.
"""

from __future__ import annotations

import tomllib
from pathlib import Path


def load_loom_config() -> dict:
    """Load ``loom.toml`` from cwd or the package root; return ``{}`` on miss."""
    candidates = [
        Path.cwd() / "loom.toml",
        Path(__file__).parents[2] / "loom.toml",
    ]
    for path in candidates:
        if path.exists():
            with open(path, "rb") as fh:
                return tomllib.load(fh)
    return {}
