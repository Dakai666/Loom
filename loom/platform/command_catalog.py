"""Shared command catalog helpers for CLI, TUI, and Discord surfaces."""

from __future__ import annotations

from pathlib import Path


def discover_personality_names(personalities_dir: str | Path = "personalities") -> list[str]:
    """Return available personality names from a directory of ``*.md`` files."""
    root = Path(personalities_dir)
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.md") if p.is_file())


def personality_slash_commands(
    personalities_dir: str | Path = "personalities",
) -> list[str]:
    """Return slash-command completions for runtime personality switching."""
    return [
        "/personality",
        "/personality off",
        *[
            f"/personality {name}"
            for name in discover_personality_names(personalities_dir)
        ],
    ]


def personality_command_entries(
    personalities_dir: str | Path = "personalities",
) -> list[tuple[str, str]]:
    """Return CLI completer entries for personality commands."""
    return [
        ("/personality", "show active persona + available list"),
        ("/personality off", "clear active persona"),
        *[
            (f"/personality {name}", f"switch persona -> {name}")
            for name in discover_personality_names(personalities_dir)
        ],
    ]
