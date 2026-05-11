"""
Theme preference persistence — read/write ~/.config/loom/theme.toml.

Issue #358.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_THEME_DIR = Path.home() / ".config" / "loom"
_THEME_FILE = _THEME_DIR / "theme.toml"
_DEFAULT_THEME = "parchment"


def load_preference() -> str:
    """
    Return the currently saved theme name, or 'parchment' if not set / file missing.
    Does not raise on corrupt TOML — falls back to default.
    """
    if not _THEME_FILE.exists():
        return _DEFAULT_THEME
    try:
        with _THEME_FILE.open("rb") as f:
            data = tomllib.load(f)
        name = data.get("theme", {}).get("name", _DEFAULT_THEME)
        if name in available_themes():
            return name
        return _DEFAULT_THEME
    except Exception:
        # Corrupt or unreadable — safest to return default
        return _DEFAULT_THEME


def save_preference(name: str) -> None:
    """
    Persist a theme name to theme.toml.
    Creates ~/.config/loom/ and the file if they don't exist.
    """
    _THEME_DIR.mkdir(parents=True, exist_ok=True)
    content = f"# Loom theme preference — do not edit manually\n[theme]\nname = {name!r}\n"
    with _THEME_FILE.open("w") as f:
        f.write(content)


def available_themes() -> list[str]:
    """Return the list of registered theme names."""
    from loom.platform.cli.theme import THEMES
    return list(THEMES.keys())
