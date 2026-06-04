#!/usr/bin/env python3
"""
Migrate inline autonomy schedules out of ``loom.toml`` (issue #444).

Moves every ``[[autonomy.schedules]]`` / ``[[autonomy.triggers]]`` block (and
their ``[autonomy.schedules.target]`` sub-tables) from ``loom.toml`` into a
sibling ``autonomy/schedules.toml``, rewriting the table headers to the
registry's top-level form:

    [[autonomy.schedules]]        ->  [[schedules]]
    [autonomy.schedules.target]   ->  [schedules.target]
    [[autonomy.triggers]]         ->  [[triggers]]

The move is a *verbatim text transform*, not a TOML re-serialisation — the giant
multi-line ``intent = \"\"\"...\"\"\"`` strings survive byte-for-byte. ``loom.toml``
is backed up to ``loom.toml.bak`` (already gitignored) before it's rewritten.

Idempotent: refuses to overwrite an existing ``autonomy/schedules.toml``; a
loom.toml with no inline blocks is a no-op.

Usage:
    python -m loom.autonomy.migrate_schedules [path/to/loom.toml]   # default: ./loom.toml
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# A schedules/triggers region: the block header itself, or any sub-table of it
# (e.g. ``[autonomy.schedules.target]``, ``[[autonomy.schedules.scope_grants]]``).
_REGION_HEADER = re.compile(r"^\s*\[\[?autonomy\.(schedules|triggers)\b")
_ANY_HEADER = re.compile(r"^\s*\[")


def _rewrite_header(line: str) -> str:
    """Strip the leading ``autonomy.`` from a schedules/triggers table header."""
    return re.sub(r"(\[\[?)autonomy\.(schedules|triggers)", r"\1\2", line, count=1)


def extract_blocks(text: str) -> tuple[str, str]:
    """Split *text* into (remaining loom.toml, extracted registry).

    A line belongs to the registry while we're inside a schedules/triggers
    region. We enter on a region header and stay until a header that is *not*
    part of a schedules/triggers table appears (or EOF). Trailing blank lines of
    a region are left with the registry so loom.toml doesn't accrue dangling gaps.
    """
    remaining: list[str] = []
    registry: list[str] = []
    in_region = False

    for line in text.splitlines(keepends=True):
        is_header = bool(_ANY_HEADER.match(line))
        if is_header:
            in_region = bool(_REGION_HEADER.match(line))
        if in_region:
            registry.append(_rewrite_header(line) if is_header else line)
        else:
            remaining.append(line)

    return "".join(remaining), "".join(registry)


def migrate(loom_path: Path) -> int:
    """Perform the migration. Returns the number of blocks moved (0 = no-op)."""
    if not loom_path.exists():
        print(f"error: {loom_path} not found", file=sys.stderr)
        return -1

    text = loom_path.read_text(encoding="utf-8")
    remaining, registry = extract_blocks(text)

    n_blocks = len(re.findall(r"^\s*\[\[autonomy\.(schedules|triggers)\]\]", text, re.M))
    if n_blocks == 0:
        print("nothing to migrate: no inline [[autonomy.schedules]]/[[autonomy.triggers]] blocks")
        return 0

    out_dir = loom_path.resolve().parent / "autonomy"
    out_path = out_dir / "schedules.toml"
    if out_path.exists():
        print(
            f"error: {out_path} already exists — refusing to overwrite. "
            "Merge by hand or remove it first.",
            file=sys.stderr,
        )
        return -1

    header = (
        "# Autonomy schedule registry — split out of loom.toml (issue #444).\n"
        "# The master on/off switch ([autonomy] enabled) stays in loom.toml;\n"
        "# only the work items live here. Agents edit THIS file, not loom.toml.\n"
        "\n"
    )

    backup = loom_path.with_suffix(loom_path.suffix + ".bak")
    backup.write_text(text, encoding="utf-8")

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + registry.lstrip("\n"), encoding="utf-8")
    loom_path.write_text(remaining.rstrip("\n") + "\n", encoding="utf-8")

    print(f"migrated {n_blocks} block(s) -> {out_path}")
    print(f"backup written -> {backup}")
    return n_blocks


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("loom.toml")
    raise SystemExit(0 if migrate(target) >= 0 else 1)
