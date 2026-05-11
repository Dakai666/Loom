"""
Loom CLI theme — pluggable palette registry.

Issue #358 (Theme System). Replaces the single hardcoded PARCHMENT palette
with a named-registry approach so adding a new theme is one dict entry.

Use semantic tokens at call sites instead of raw colour names so a
future palette tweak is one-file.

Semantic tokens (stable ABI):
    loom.text / loom.muted / loom.accent / loom.success / loom.warning /
    loom.error / loom.border / loom.harness.bg / loom.harness.signature /
    loom.agent.guide
"""

from __future__ import annotations

from rich.theme import Theme

# ---------------------------------------------------------------------------
# Raw palettes — sole source of hex values.
# ---------------------------------------------------------------------------

PARCHMENT_BG       = "#1c1814"
PARCHMENT_SURFACE  = "#242018"
PARCHMENT_TEXT     = "#e0cfa0"
PARCHMENT_MUTED    = "#8a7a5e"
PARCHMENT_ACCENT   = "#c8a464"
PARCHMENT_SUCCESS  = "#7a9e78"
PARCHMENT_WARNING  = "#c8924a"
PARCHMENT_ERROR    = "#b87060"
PARCHMENT_BORDER   = "#4a4038"

SUNRISE_BG         = "#0d1117"
SUNRISE_SURFACE    = "#161b22"
SUNRISE_TEXT       = "#e6f0ff"
SUNRISE_MUTED      = "#6e8898"
SUNRISE_ACCENT     = "#FFD54F"
SUNRISE_SUCCESS    = "#81D4FA"
SUNRISE_WARNING    = "#FFAB40"
SUNRISE_ERROR      = "#FF8A80"
SUNRISE_BORDER     = "#2d3748"

# ---------------------------------------------------------------------------
# Palette registry.
# ---------------------------------------------------------------------------

THEMES: dict[str, dict[str, str]] = {
    "parchment": {
        "bg":        PARCHMENT_BG,
        "surface":   PARCHMENT_SURFACE,
        "text":      PARCHMENT_TEXT,
        "muted":     PARCHMENT_MUTED,
        "accent":    PARCHMENT_ACCENT,
        "success":   PARCHMENT_SUCCESS,
        "warning":   PARCHMENT_WARNING,
        "error":     PARCHMENT_ERROR,
        "border":    PARCHMENT_BORDER,
    },
    "sunrise": {
        "bg":        SUNRISE_BG,
        "surface":   SUNRISE_SURFACE,
        "text":      SUNRISE_TEXT,
        "muted":     SUNRISE_MUTED,
        "accent":    SUNRISE_ACCENT,
        "success":   SUNRISE_SUCCESS,
        "warning":   SUNRISE_WARNING,
        "error":     SUNRISE_ERROR,
        "border":    SUNRISE_BORDER,
    },
}


def _make_tokens(palette: dict[str, str]) -> dict[str, str]:
    """Build a Rich Theme token dict from a palette dict."""
    return {
        "loom.text":               palette["text"],
        "loom.muted":              palette["muted"],
        "loom.accent":             palette["accent"],
        "loom.success":            palette["success"],
        "loom.warning":            palette["warning"],
        "loom.error":              palette["error"],
        "loom.border":             palette["border"],
        "loom.harness.bg":         f"on {palette['surface']}",
        "loom.harness.signature":  palette["accent"],
        "loom.agent.guide":        palette["accent"],
        # Convenience composites
        "loom.accent.bold":        f"bold {palette['accent']}",
        "loom.muted.italic":       f"italic {palette['muted']}",
    }


def build_theme(name: str = "parchment") -> Theme:
    """Build a Rich Theme from a registered palette name."""
    if name not in THEMES:
        raise ValueError(f"Unknown theme '{name}'. Available: {list(THEMES.keys())}")
    return Theme(_make_tokens(THEMES[name]))


def _active_theme_name() -> str:
    """Return the persisted theme preference, falling back to parchment."""
    try:
        from loom.platform.cli.theme_persist import load_preference

        return load_preference()
    except Exception:
        return "parchment"


def active_palette() -> dict[str, str]:
    """Return a copy of the palette selected by persisted preference."""
    return dict(THEMES[_active_theme_name()])


# ---------------------------------------------------------------------------
# Default theme (used before any preference is loaded).
# ---------------------------------------------------------------------------

LOOM_THEME = build_theme(_active_theme_name())


__all__ = [
    "THEMES",
    "active_palette",
    "build_theme",
    "LOOM_THEME",
    # Raw parchment constants (kept for TUI layer compatibility)
    "PARCHMENT_BG",
    "PARCHMENT_SURFACE",
    "PARCHMENT_TEXT",
    "PARCHMENT_MUTED",
    "PARCHMENT_ACCENT",
    "PARCHMENT_SUCCESS",
    "PARCHMENT_WARNING",
    "PARCHMENT_ERROR",
    "PARCHMENT_BORDER",
    # Raw sunrise constants
    "SUNRISE_BG",
    "SUNRISE_SURFACE",
    "SUNRISE_TEXT",
    "SUNRISE_MUTED",
    "SUNRISE_ACCENT",
    "SUNRISE_SUCCESS",
    "SUNRISE_WARNING",
    "SUNRISE_ERROR",
    "SUNRISE_BORDER",
]
