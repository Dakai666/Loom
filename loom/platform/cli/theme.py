"""
Loom CLI theme — single source of truth for parchment palette.

Issue #236 (CLI Refresh, PR-B). Pulls the colour palette already defined
in the TUI layer (``loom/platform/cli/tui/app.py``) into a Rich-native
:class:`Theme` so plain CLI surfaces share the same visual language.

Use semantic tokens at call sites instead of raw colour names so a
future palette tweak is one-file.

Tokens
------
Foreground
    loom.text     — parchment cream, the default body text
    loom.muted    — dim/secondary text (replaces the pervasive ``[dim]``)
    loom.accent   — amber gold, focal highlights and active state
    loom.success  — sage green, completion / approve / ok
    loom.warning  — ochre, attention but not failure
    loom.error    — terracotta, failure or denial
    loom.border   — subtle frame colour for panels
Surfaces
    loom.harness.bg  — dark surface for harness messages (PR-C consumer)
"""

from __future__ import annotations

from rich.theme import Theme

# ---------------------------------------------------------------------------
# Raw palette — sole source of hex values. Mirrors the comment block in
# loom/platform/cli/tui/app.py so the two layers stay aligned.
# ---------------------------------------------------------------------------

PARCHMENT_BG       = "#1c1814"  # screen background (very dark warm brown)
PARCHMENT_SURFACE  = "#242018"  # widget surface
PARCHMENT_TEXT     = "#e0cfa0"  # primary text (warm cream)
PARCHMENT_MUTED    = "#8a7a5e"  # muted text
PARCHMENT_ACCENT   = "#c8a464"  # accent (amber gold)
PARCHMENT_SUCCESS  = "#7a9e78"  # success (sage green)
PARCHMENT_WARNING  = "#c8924a"  # warning (ochre)
PARCHMENT_ERROR    = "#b87060"  # error (terracotta)
PARCHMENT_BORDER   = "#4a4038"  # border


# ---------------------------------------------------------------------------
# Semantic theme — what call sites should reference.
# ---------------------------------------------------------------------------

LOOM_THEME = Theme(
    {
        # Foreground
        "loom.text":        PARCHMENT_TEXT,
        "loom.muted":       PARCHMENT_MUTED,
        "loom.accent":      PARCHMENT_ACCENT,
        "loom.success":     PARCHMENT_SUCCESS,
        "loom.warning":     PARCHMENT_WARNING,
        "loom.error":       PARCHMENT_ERROR,
        "loom.border":      PARCHMENT_BORDER,
        # Surfaces
        "loom.harness.bg":  f"on {PARCHMENT_SURFACE}",
        # Convenience composites — emphasis variants used at multiple call
        # sites. Add sparingly; prefer composing tokens at the call site.
        "loom.accent.bold": f"bold {PARCHMENT_ACCENT}",
        "loom.muted.italic": f"italic {PARCHMENT_MUTED}",
    }
)


__all__ = [
    "LOOM_THEME",
    "PARCHMENT_BG",
    "PARCHMENT_SURFACE",
    "PARCHMENT_TEXT",
    "PARCHMENT_MUTED",
    "PARCHMENT_ACCENT",
    "PARCHMENT_SUCCESS",
    "PARCHMENT_WARNING",
    "PARCHMENT_ERROR",
    "PARCHMENT_BORDER",
]
