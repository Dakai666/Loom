"""Public loader for ``loom.toml`` runtime configuration.

Single source of truth for the file-system search + parse logic. Other
layers (session, autonomy, cognition) import ``load_loom_config`` instead
of reaching into private session-module helpers — see #306.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from loom.core.infra.log_once import log_once

logger = logging.getLogger(__name__)

_RETIRED_PLUGIN_KEYS = ("plugins", "adapter", "adapters")


def _warn_retired_config_keys(cfg: dict) -> None:
    present = [k for k in _RETIRED_PLUGIN_KEYS if k in cfg]
    if present:
        log_once(
            logger, logging.WARNING, ("retired_config_keys", tuple(present)),
            "loom.toml contains retired plugin/adapter section(s) %s "
            "(retired in #708 S4a); plugins and adapters are no longer loaded "
            "— remove these keys or migrate to [[mcp.servers]].",
            ", ".join(f"[{k}]" for k in present),
        )


def load_loom_config() -> dict:
    """Load ``loom.toml`` from cwd or the package root; return ``{}`` on miss."""
    candidates = [
        Path.cwd() / "loom.toml",
        Path(__file__).parents[2] / "loom.toml",
    ]
    for path in candidates:
        if path.exists():
            with open(path, "rb") as fh:
                cfg = tomllib.load(fh)
            if isinstance(cfg, dict):
                _warn_retired_config_keys(cfg)
            return cfg
    return {}
