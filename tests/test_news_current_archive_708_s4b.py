"""#708 S4b Item 6: news/current active write boundary + safe archive_news.py.

Contracts pinned here:
- ``skill_allows._check_write`` and ``_SCAN_LIMIT = 2000`` are NOT relaxed:
  hidden files (.DS_Store), symlinks, and >2000 entries inside ``news/current``
  are still rejected.
- Simulating live ``news/`` (1,325+ historical files across 225+ dated folders
  AND Finder ``news/.DS_Store``) loads cleanly when ``news-aggregator`` uses
  ``allows.write: [news/current]`` and passes the real permission harness.
- ``skills/news-aggregator/scripts/archive_news.py`` moves (never deletes)
  dated folders older than ``--keep-days`` from ``news/current/`` to
  ``news/archive/``, enforces workspace boundaries, refuses symlinks, never
  overwrites archive collisions, and exposes no arbitrary path flags on CLI.
- ``skills/news-aggregator/checks.py`` accepts ``news/current/YYYY-MM-DD``
  (mkdir + cover.jpg curl) while keeping legacy patterns and rejecting escapes.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from loom.autonomy import skill_allows
from loom.autonomy.chime import ChimeRequest
from loom.autonomy.schedules import load_schedules
from loom.autonomy.skill_allows import SkillAllowsError, read_skill_allows
from loom.core.harness.mode import PermissionMode
from loom.core.harness.permissions import ToolCapability
from tests.test_activity_skills_708 import apply
from tests.test_approval_classifier_690_s6 import ledger, _no_network  # noqa: F401
from tests.test_permission_mode_704 import _env
from tests.test_single_decision_704_s3 import _call

REPO = Path(__file__).resolve().parents[1]
TRACKED_SKILL = REPO / "skills" / "news-aggregator"
ARCHIVE_SCRIPT = TRACKED_SKILL / "scripts" / "archive_news.py"


def _install_tracked_news_skill(ws: Path) -> Path:
    dest = ws / "skills" / "news-aggregator"
    (dest / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TRACKED_SKILL / "SKILL.md", dest / "SKILL.md")
    shutil.copyfile(TRACKED_SKILL / "checks.py", dest / "checks.py")
    (dest / "scripts" / "fetch_news.py").write_text("# synthetic stub\n", encoding="utf-8")
    assert ARCHIVE_SCRIPT.is_file(), "archive_news.py is missing"
    shutil.copyfile(ARCHIVE_SCRIPT, dest / "scripts" / "archive_news.py")
    return dest


def _load_archive_module(script_path: Path):
    spec = importlib.util.spec_from_file_location("archive_news_test_mod", script_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_checks_module():
    spec = importlib.util.spec_from_file_location("news_checks_test_mod", TRACKED_SKILL / "checks.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _populate_synthetic_live_news(ws: Path, *, days: int = 230, files_per_day: int = 5) -> int:
    """Create 1,380+ historical entries directly under news/ plus Finder .DS_Store."""
    news_dir = ws / "news"
    news_dir.mkdir(parents=True, exist_ok=True)
    (news_dir / ".DS_Store").write_bytes(b"synthetic finder metadata")
    archive_dir = news_dir / "archive"
    archive_dir.mkdir(exist_ok=True)
    (archive_dir / ".DS_Store").write_bytes(b"synthetic archive finder metadata")

    total_entries = 0
    for idx in range(days):
        month = 1 + (idx // 28)
        day = 1 + (idx % 28)
        d = news_dir / f"2026-{month:02d}-{day:02d}"
        d.mkdir(exist_ok=True)
        total_entries += 1
        for f_idx in range(files_per_day):
            (d / f"track_{f_idx}.md").write_text("synthetic\n", encoding="utf-8")
            total_entries += 1
    assert total_entries >= 1325
    return total_entries


def test_scan_limit_and_check_write_guards_remain_unrelaxed(tmp_path):
    assert skill_allows._SCAN_LIMIT == 2000
    _install_tracked_news_skill(tmp_path)

    # Hidden file inside active news/current MUST still be rejected.
    cur = tmp_path / "news" / "current"
    cur.mkdir(parents=True)
    ds = cur / ".DS_Store"
    ds.write_bytes(b"finder")
    with pytest.raises(SkillAllowsError, match="hidden_path"):
        read_skill_allows("news-aggregator", tmp_path)
    ds.unlink()

    # Symlink inside active news/current MUST still be rejected.
    link = cur / "escape"
    link.symlink_to(tmp_path)
    with pytest.raises(SkillAllowsError, match="symlink"):
        read_skill_allows("news-aggregator", tmp_path)
    link.unlink()

    # >2000 entries inside active news/current MUST still be rejected.
    for i in range(2001):
        (cur / f"f_{i:04d}.md").write_text("x", encoding="utf-8")
    with pytest.raises(SkillAllowsError, match="2000"):
        read_skill_allows("news-aggregator", tmp_path)


@pytest.mark.parametrize("mode", list(PermissionMode))
@pytest.mark.parametrize("origin", ["autonomy", "chime"])
async def test_news_current_loads_and_runs_through_harness_despite_1325_history_and_ds_store(
    tmp_path, ledger, monkeypatch, mode, origin
):
    env = _env(tmp_path, ledger, mode=mode)
    monkeypatch.chdir(env.ws)
    _install_tracked_news_skill(env.ws)
    _populate_synthetic_live_news(env.ws, days=230, files_per_day=5)
    # Create a few recent active briefings inside news/current.
    today_dir = env.ws / "news" / "current" / "2026-11-27"
    today_dir.mkdir(parents=True)
    (today_dir / "tech_briefing.md").write_text("existing\n", encoding="utf-8")

    boundary = read_skill_allows("news-aggregator", env.ws)
    assert boundary.write == ("news/current",)
    assert "python3 skills/news-aggregator/scripts/fetch_news.py" in boundary.exec
    assert "python3 skills/news-aggregator/scripts/archive_news.py" in boundary.exec

    sched_path = env.ws / "autonomy" / "schedules.toml"
    sched_path.parent.mkdir(parents=True, exist_ok=True)
    sched_path.write_text(
        '[[schedules]]\nname = "morning_briefing"\ncron = "0 1 * * *"\n'
        'intent = "synthetic"\nskills = ["news-aggregator"]\n'
        'allowed_tools = ["write_file"]\n',
        encoding="utf-8",
    )
    items = load_schedules(sched_path)["schedules"]
    assert len(items) == 1
    item = items[0]

    req = ChimeRequest(
        input_trust="local",
        schedule_name=item["name"],
        intent=item["intent"],
        fired_at=datetime.now(timezone.utc),
        allowed_tools=tuple(item["allowed_tools"]),
        scope_grants=tuple(item["scope_grants"]),
    )
    apply(env, req)

    # Active write inside news/current succeeds with zero confirmations.
    ok_write = await env.run(
        "write_file",
        {"path": "news/current/2026-11-27/daily_briefing.md", "content": "# briefing\n"},
        origin=origin,
    )
    assert ok_write.success

    # Historical news/ and news/archive/ and traversal and hidden files are all refused.
    for blocked in [
        "news/2026-01-01/daily_briefing.md",
        "news/archive/2026-01-01/daily_briefing.md",
        "news/current/../2026-01-01/daily_briefing.md",
        "news/current/.DS_Store",
    ]:
        res = await env.run("write_file", {"path": blocked, "content": "nope"}, origin=origin)
        assert not res.success, f"expected {blocked} to be blocked"

    # Archiver script invocation is authorized under the skill's exec grant; absolute paths denied.
    ok_exec = await _call(
        env,
        "run_bash",
        {"command": "python3 skills/news-aggregator/scripts/archive_news.py --keep-days 30"},
        origin=origin,
        caps=ToolCapability.EXEC,
    )
    assert ok_exec.success
    bad_exec = await _call(
        env,
        "run_bash",
        {"command": "python3 skills/news-aggregator/scripts/archive_news.py /etc/passwd"},
        origin=origin,
        caps=ToolCapability.EXEC,
    )
    assert not bad_exec.success
    assert env.confirm.await_count == 0


def test_archive_news_moves_old_keeps_recent_and_never_deletes(tmp_path):
    skill_dir = _install_tracked_news_skill(tmp_path)
    mod = _load_archive_module(skill_dir / "scripts" / "archive_news.py")

    current = tmp_path / "news" / "current"
    old_dir = current / "2026-10-01"
    edge_dir = current / "2026-10-28"  # exactly 30 days before 2026-11-27 -> kept
    recent_dir = current / "2026-11-27"
    non_date_dir = current / "notes"
    for d, payload in [
        (old_dir, "oct-1 briefing"),
        (edge_dir, "edge briefing"),
        (recent_dir, "today briefing"),
        (non_date_dir, "misc note"),
    ]:
        d.mkdir(parents=True)
        (d / "daily_briefing.md").write_text(payload, encoding="utf-8")

    # Dry run changes nothing.
    dry = mod.archive_old_news(tmp_path, keep_days=30, today=date(2026, 11, 27), dry_run=True)
    assert dry.moved == ["2026-10-01"]
    assert old_dir.exists()
    assert not (tmp_path / "news" / "archive" / "2026-10-01").exists()

    # Real run moves only strictly older dated directories, preserving exact contents.
    res = mod.archive_old_news(tmp_path, keep_days=30, today=date(2026, 11, 27))
    assert res.moved == ["2026-10-01"]
    assert sorted(res.kept) == ["2026-10-28", "2026-11-27"]
    assert any("notes" in s for s in res.skipped)

    assert not old_dir.exists()
    archived = tmp_path / "news" / "archive" / "2026-10-01" / "daily_briefing.md"
    assert archived.read_text(encoding="utf-8") == "oct-1 briefing"
    assert (edge_dir / "daily_briefing.md").read_text(encoding="utf-8") == "edge briefing"
    assert (recent_dir / "daily_briefing.md").read_text(encoding="utf-8") == "today briefing"
    assert (non_date_dir / "daily_briefing.md").read_text(encoding="utf-8") == "misc note"


def test_archive_news_handles_missing_current_collisions_and_symlinks(tmp_path):
    skill_dir = _install_tracked_news_skill(tmp_path)
    mod = _load_archive_module(skill_dir / "scripts" / "archive_news.py")

    # Missing news/current is idempotent and succeeds.
    empty = mod.archive_old_news(tmp_path, keep_days=30, today=date(2026, 11, 27))
    assert empty.moved == []

    current = tmp_path / "news" / "current"
    archive = tmp_path / "news" / "archive"
    current.mkdir(parents=True)
    archive.mkdir(parents=True)

    # Collision: existing archive folder must NOT be overwritten or deleted.
    (current / "2026-09-01").mkdir()
    (current / "2026-09-01" / "a.md").write_text("from-current", encoding="utf-8")
    (archive / "2026-09-01").mkdir()
    (archive / "2026-09-01" / "a.md").write_text("from-archive", encoding="utf-8")

    # Symlinked date folder and nested symlink inside date folder must be skipped.
    outside = tmp_path / "outside"
    outside.mkdir()
    (current / "2026-09-02").symlink_to(outside)
    nested = current / "2026-09-03"
    nested.mkdir()
    (nested / "link.md").symlink_to(outside)

    res = mod.archive_old_news(tmp_path, keep_days=30, today=date(2026, 11, 27))
    assert res.moved == []
    assert len(res.skipped) == 3
    assert (current / "2026-09-01" / "a.md").read_text(encoding="utf-8") == "from-current"
    assert (archive / "2026-09-01" / "a.md").read_text(encoding="utf-8") == "from-archive"
    assert (current / "2026-09-02").is_symlink()
    assert nested.exists()


def test_archive_news_refuses_symlinked_roots_and_cli_boundary_flags(tmp_path):
    skill_dir = _install_tracked_news_skill(tmp_path)
    script = skill_dir / "scripts" / "archive_news.py"

    # CLI refuses --keep-days 0 and refuses unknown path flags (--src/--dst/--workspace).
    for bad_args in (["--keep-days", "0"], ["--keep-days", "-1"], ["--workspace", str(tmp_path)]):
        proc = subprocess.run(
            [sys.executable, str(script), *bad_args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 2, f"expected argparse refusal for {bad_args}: {proc.stderr}"

    # Symlinked news/current root is refused by CLI with non-zero exit.
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (tmp_path / "news").mkdir()
    (tmp_path / "news" / "current").symlink_to(outside)
    proc = subprocess.run(
        [sys.executable, str(script), "--keep-days", "30"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0
    assert "symlink" in (proc.stderr + proc.stdout).lower()

    # Real CLI run on clean workspace succeeds and prints valid JSON.
    (tmp_path / "news" / "current").unlink()
    old = tmp_path / "news" / "current" / "2020-01-01"
    old.mkdir(parents=True)
    (old / "daily_briefing.md").write_text("vintage\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script), "--keep-days", "30"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["moved"] == ["2020-01-01"]
    assert (tmp_path / "news" / "archive" / "2020-01-01" / "daily_briefing.md").read_text(
        encoding="utf-8"
    ) == "vintage\n"


async def test_news_checks_accepts_news_current_and_rejects_escapes():
    checks = _load_checks_module()

    class _CallObj:
        def __init__(self, command: str) -> None:
            self.args = {"command": command}

    url = "https://example.com/cover.jpg"
    for cmd in [
        "mkdir -p news/current/2026-11-27",
        "mkdir news/current/2026-11-27",
        f'curl -sL --max-time 30 -o news/current/2026-11-27/cover.jpg "{url}"',
        "python3 skills/news-aggregator/scripts/archive_news.py --keep-days 30",
        "mkdir -p news/2026-10-26",
        f'curl -sL -o news/2026-10-26/cover.jpg "{url}"',
    ]:
        assert await checks.scripts_only_bash(_CallObj(cmd)), f"expected allowed: {cmd}"

    for cmd in [
        f'curl -sL -o news/current/2026-11-27/../../evil.sh "{url}"',
        f'curl -sL -o news/archive/2026-11-27/cover.jpg "{url}"',
        "rm -rf news/current",
    ]:
        assert not await checks.scripts_only_bash(_CallObj(cmd)), f"expected denied: {cmd}"
