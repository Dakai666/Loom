"""
Tests for the #444 schedule-migration script.

The migration is a *text* transform, so the invariant under test is: the moved
registry round-trips through tomllib to the same data the daemon would have read
from the inline blocks, multi-line intents survive byte-for-byte, and the
non-autonomy sections of loom.toml are left untouched.
"""

import tomllib

from loom.autonomy.migrate_schedules import extract_blocks, migrate

SAMPLE = '''\
[cognition]
model = "claude"

[autonomy]
enabled = true

[autonomy.circadian]
enabled = true

[[autonomy.schedules]]
name = "morning_briefing"
cron = "0 1 * * *"
intent = """
Line one.

  Indented line — must survive verbatim.
"""
trust_level = "safe"
mode = "chime"
allowed_tools = ["write_file"]

  [autonomy.schedules.target]
  type = "discord_thread"
  id = "123456789"
  fallback = "skip"

[[autonomy.schedules]]
name = "daily_journal"
cron = "30 15 * * *"
intent = "Write today's journal."
trust_level = "safe"

[[autonomy.triggers]]
name = "on_error"
event = "error_spike"
intent = "Investigate."
'''


def test_extract_preserves_non_autonomy_sections():
    remaining, _ = extract_blocks(SAMPLE)
    parsed = tomllib.loads(remaining)
    assert parsed["cognition"]["model"] == "claude"
    assert parsed["autonomy"]["enabled"] is True
    assert parsed["autonomy"]["circadian"]["enabled"] is True
    # The inline schedule arrays must be gone from loom.toml.
    assert "schedules" not in parsed.get("autonomy", {})
    assert "triggers" not in parsed.get("autonomy", {})


def test_extract_rewrites_headers_to_registry_form():
    _, registry = extract_blocks(SAMPLE)
    parsed = tomllib.loads(registry)
    assert [s["name"] for s in parsed["schedules"]] == [
        "morning_briefing",
        "daily_journal",
    ]
    assert parsed["triggers"][0]["event"] == "error_spike"
    # Nested target sub-table rewritten and still bound to its parent block.
    assert parsed["schedules"][0]["target"]["id"] == "123456789"


def test_multiline_intent_survives_verbatim():
    _, registry = extract_blocks(SAMPLE)
    parsed = tomllib.loads(registry)
    intent = parsed["schedules"][0]["intent"]
    assert "Indented line — must survive verbatim." in intent
    assert intent.count("\n") >= 3


def test_migrate_end_to_end(tmp_path):
    loom = tmp_path / "loom.toml"
    loom.write_text(SAMPLE, encoding="utf-8")

    n = migrate(loom)
    assert n == 3

    registry = tmp_path / "autonomy" / "schedules.toml"
    assert registry.exists()
    parsed = tomllib.loads(registry.read_text(encoding="utf-8"))
    assert len(parsed["schedules"]) == 2
    assert len(parsed["triggers"]) == 1

    # loom.toml still valid, autonomy master switch intact, inline blocks gone.
    loom_parsed = tomllib.loads(loom.read_text(encoding="utf-8"))
    assert loom_parsed["autonomy"]["enabled"] is True
    assert "schedules" not in loom_parsed["autonomy"]

    # Backup captured the pre-migration file.
    assert (tmp_path / "loom.toml.bak").exists()


def test_migrate_second_run_is_noop(tmp_path):
    loom = tmp_path / "loom.toml"
    loom.write_text(SAMPLE, encoding="utf-8")
    assert migrate(loom) == 3
    # loom.toml is now clean → second run finds nothing to move (no-op).
    assert migrate(loom) == 0


def test_migrate_refuses_to_clobber_existing_registry(tmp_path):
    # Registry already present AND loom.toml still has inline blocks (e.g. a
    # half-finished migration): refuse rather than overwrite the registry.
    loom = tmp_path / "loom.toml"
    loom.write_text(SAMPLE, encoding="utf-8")
    (tmp_path / "autonomy").mkdir()
    (tmp_path / "autonomy" / "schedules.toml").write_text("# pre-existing\n", encoding="utf-8")
    assert migrate(loom) == -1
    # loom.toml left untouched (still has its inline blocks).
    assert "[[autonomy.schedules]]" in loom.read_text(encoding="utf-8")


def test_migrate_noop_when_no_inline_blocks(tmp_path):
    loom = tmp_path / "loom.toml"
    loom.write_text("[autonomy]\nenabled = true\n", encoding="utf-8")
    assert migrate(loom) == 0
    assert not (tmp_path / "autonomy" / "schedules.toml").exists()
