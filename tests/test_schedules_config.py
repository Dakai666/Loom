"""
Contract tests for the autonomy schedule registry loader (issue #444).

The loader is the seam between ``loom.toml`` (system posture) and
``autonomy/schedules.toml`` (work items). Its lifeline invariant: it must never
raise — every failure mode the daemon should survive degrades to empty arrays,
and a single bad entry is dropped individually rather than failing the file.
"""

from pathlib import Path

from loom.autonomy.schedules import (
    DEFAULT_SCHEDULES_PATH,
    load_schedules,
)


def test_missing_file_returns_empty_arrays(tmp_path):
    got = load_schedules(tmp_path / "schedules.toml")
    assert got == {"schedules": [], "triggers": []}


def test_default_path_is_workspace_relative():
    # Same isolation model as the circadian rhythm table: cwd-anchored.
    assert DEFAULT_SCHEDULES_PATH == Path("autonomy/schedules.toml")


def test_parses_schedules_and_triggers(tmp_path):
    f = tmp_path / "schedules.toml"
    f.write_text(
        """
[[schedules]]
name = "daily_review"
cron = "0 9 * * 1-5"
intent = "Review progress"
trust_level = "guarded"

[[schedules]]
name = "weekly_prune"
cron = "0 2 * * 0"
intent = "Prune"
trust_level = "safe"

[[triggers]]
name = "on_error_spike"
event = "error_rate_threshold"
intent = "Analyse errors"
""",
        encoding="utf-8",
    )
    got = load_schedules(f)
    assert [s["name"] for s in got["schedules"]] == ["daily_review", "weekly_prune"]
    assert got["triggers"][0]["event"] == "error_rate_threshold"


def test_nested_target_table_preserved(tmp_path):
    # chime schedules carry a [schedules.target] sub-table — must survive intact.
    f = tmp_path / "schedules.toml"
    f.write_text(
        """
[[schedules]]
name = "morning_briefing"
cron = "0 1 * * *"
intent = "briefing"
mode = "chime"

  [schedules.target]
  type = "discord_thread"
  id = "1505984613351428196"
  fallback = "skip"
""",
        encoding="utf-8",
    )
    got = load_schedules(f)
    target = got["schedules"][0]["target"]
    assert target["type"] == "discord_thread"
    assert target["id"] == "1505984613351428196"


def test_invalid_toml_returns_empty(tmp_path):
    f = tmp_path / "schedules.toml"
    f.write_text("this is = = not valid toml [[[", encoding="utf-8")
    assert load_schedules(f) == {"schedules": [], "triggers": []}


def test_non_array_key_coerced_to_empty(tmp_path):
    f = tmp_path / "schedules.toml"
    f.write_text('schedules = "oops not an array"\n', encoding="utf-8")
    got = load_schedules(f)
    assert got["schedules"] == []
    assert got["triggers"] == []


def test_non_table_entry_skipped_individually(tmp_path):
    # A stray scalar in the array must not nuke the valid sibling entries.
    # (array-of-tables can't mix scalars in TOML syntax, so build via a raw
    # inline array to exercise the per-entry guard.)
    f = tmp_path / "schedules.toml"
    f.write_text(
        'schedules = [ { name = "ok", cron = "* * * * *", intent = "x" }, "bogus" ]\n',
        encoding="utf-8",
    )
    got = load_schedules(f)
    assert [s["name"] for s in got["schedules"]] == ["ok"]


def test_empty_file_returns_empty_arrays(tmp_path):
    f = tmp_path / "schedules.toml"
    f.write_text("", encoding="utf-8")
    assert load_schedules(f) == {"schedules": [], "triggers": []}
