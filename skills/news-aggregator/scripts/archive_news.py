"""Archive dated briefings older than N days from news/current/ to news/archive/.

Design invariants (#708 S4b):
- Move only, NEVER delete any briefing or overwrite an existing archive folder.
- Workspace root is derived from this script's location (no --workspace/--src/--dst
  CLI flags) so an unattended exec prefix grant cannot redirect moves elsewhere.
- Never follow symlinks: symlinked news/current/archive roots abort with error;
  symlinked date entries or date entries containing any symlink are skipped.
- Keeps the active ``news/current/`` prefix well below ``skill_allows._SCAN_LIMIT``
  (2,000 entries) and isolated from Finder ``.DS_Store`` files in ``news/`` or
  ``news/archive/`` without relaxing any security check.
"""

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class ArchiveResult:
    cutoff: str
    keep_days: int
    dry_run: bool
    moved: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _check_workspace_dir(path: Path, ws_real: Path, label: str) -> None:
    """Verify *path* is not a symlink and stays strictly inside *ws_real*."""
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {path}")
    resolved = path.resolve()
    if not resolved.is_relative_to(ws_real) or resolved == ws_real:
        raise ValueError(f"{label} escapes workspace: {path}")
    if path.exists() and not path.is_dir():
        raise ValueError(f"{label} must be a directory: {path}")


def _contains_symlink(directory: Path) -> bool:
    for root, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs + files:
            if (Path(root) / name).is_symlink():
                return True
    return False


def _parse_date_dir(name: str) -> date | None:
    if not _DATE_DIR_RE.match(name):
        return None
    try:
        return date.fromisoformat(name)
    except ValueError:
        return None


def archive_old_news(
    workspace: Path,
    *,
    keep_days: int = 30,
    today: date | None = None,
    dry_run: bool = False,
) -> ArchiveResult:
    """Move ``news/current/YYYY-MM-DD`` strictly older than *keep_days* to ``news/archive/``."""
    if isinstance(keep_days, bool) or not isinstance(keep_days, int) or keep_days < 1:
        raise ValueError(f"keep_days must be an integer >= 1, got {keep_days!r}")

    ws_real = workspace.resolve()
    news_root = ws_real / "news"
    current_dir = news_root / "current"
    archive_dir = news_root / "archive"

    _check_workspace_dir(news_root, ws_real, "news/")
    _check_workspace_dir(current_dir, ws_real, "news/current/")
    _check_workspace_dir(archive_dir, ws_real, "news/archive/")

    ref_today = today or date.today()
    cutoff = ref_today - timedelta(days=keep_days)
    result = ArchiveResult(
        cutoff=cutoff.isoformat(),
        keep_days=keep_days,
        dry_run=dry_run,
    )

    if not current_dir.exists():
        return result

    for entry in sorted(current_dir.iterdir(), key=lambda p: p.name):
        name = entry.name
        if entry.is_symlink():
            result.skipped.append(f"{name} (symlink)")
            continue
        if not entry.is_dir():
            result.skipped.append(f"{name} (not a directory)")
            continue
        entry_date = _parse_date_dir(name)
        if entry_date is None:
            result.skipped.append(f"{name} (not YYYY-MM-DD)")
            continue
        if entry_date >= cutoff:
            result.kept.append(name)
            continue
        if _contains_symlink(entry):
            result.skipped.append(f"{name} (contains symlink)")
            continue
        dst = archive_dir / name
        if dst.exists() or dst.is_symlink():
            result.skipped.append(f"{name} (archive target already exists)")
            continue
        if not dry_run:
            archive_dir.mkdir(parents=True, exist_ok=True)
            entry.rename(dst)
        result.moved.append(name)

    return result


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("--keep-days must be >= 1")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Move dated briefing folders older than --keep-days from "
            "news/current/ to news/archive/ (move-only, never deletes)."
        ),
    )
    parser.add_argument(
        "--keep-days",
        type=_positive_int,
        default=30,
        help="Number of recent days to keep in news/current/ (default: 30, min: 1).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report which folders would be archived without moving anything.",
    )
    args = parser.parse_args(argv)

    # Workspace is fixed to the parent tree of skills/news-aggregator/scripts/archive_news.py.
    workspace = Path(__file__).resolve().parents[3]
    try:
        result = archive_old_news(workspace, keep_days=args.keep_days, dry_run=args.dry_run)
    except (ValueError, OSError) as exc:
        print(f"archive_news error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
