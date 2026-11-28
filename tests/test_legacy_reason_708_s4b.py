"""#708 S4b: schedule legacy_reason and open-anchor compatibility.

Contracts pinned here:
- Legacy ``scope_grants`` without ``skills`` and with a valid non-empty
  ``legacy_reason`` logs at ``INFO`` (naming both the activity and reason)
  instead of ``WARNING``, while preserving the loaded grants.
- Legacy ``scope_grants`` without ``skills`` and without ``legacy_reason``
  (genuinely unmigrated) keeps logging at ``WARNING``.
- Invalid ``legacy_reason`` (non-string or blank/whitespace string) rejects
  only that entry with ``ERROR``, preserving healthy siblings.
- Combining ``skills`` and ``legacy_reason`` (semantic contradiction) rejects
  only that entry with ``ERROR``, preserving healthy siblings.
- Rhythm open slots ignore legacy_reason, including malformed values;
  their example carries no permission fields. Schedules retain this field.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

import pytest

from loom.autonomy.circadian.rhythm import load_rhythm
from loom.autonomy.schedules import load_schedules


def _make_skill(ws: Path, name: str) -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\nallows: {{}}\n---\n", encoding="utf-8")


def _write_entry(ws: Path, kind: str, extra: str) -> Path:
    if kind == "schedule":
        path = ws / "autonomy" / "schedules.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '[[schedules]]\nname = "skill_weekly_review"\ncron = "0 2 * * 6"\n'
            'intent = "synthetic"\n' + extra
            + '\n[[schedules]]\nname = "healthy"\ncron = "0 3 * * *"\nintent = "ok"\n',
            encoding="utf-8",
        )
        return path
    if kind == "trigger":
        path = ws / "autonomy" / "schedules.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '[[triggers]]\nname = "legacy_evt"\nevent = "synthetic"\n'
            'intent = "synthetic"\n' + extra
            + '\n[[triggers]]\nname = "healthy"\nevent = "ok"\nintent = "ok"\n',
            encoding="utf-8",
        )
        return path
    path = ws / "autonomy" / "circadian" / "rhythm.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[[anchors]]\ntime = "11:00"\nname = "curiosity"\nmeaning = "synthetic"\n'
        + extra
        + '\n[[anchors]]\ntime = "16:00"\nname = "healthy"\nmeaning = "ok"\n',
        encoding="utf-8",
    )
    return path


def _load(path: Path, kind: str):
    if kind == "schedule":
        return load_schedules(path)["schedules"]
    if kind == "trigger":
        return load_schedules(path)["triggers"]
    return load_rhythm(path)


def _name(item) -> str:
    return item["name"] if isinstance(item, dict) else item.name


def _grants(item):
    return item["scope_grants"] if isinstance(item, dict) else list(item.scope_grants)


@pytest.mark.parametrize("kind", ["schedule", "trigger", "anchor"])
def test_valid_legacy_reason_downgrades_warning_to_info(tmp_path, kind, caplog):
    reason = "open-ended research phase without a single skill script"
    extra = (
        f'legacy_reason = "{reason}"\n'
        'scope_grants = [{ resource = "path", action = "write", selector = "outputs" }]\n'
    )
    path = _write_entry(tmp_path, kind, extra)
    with caplog.at_level(logging.INFO, logger="loom.autonomy"):
        items = _load(path, kind)
    assert len(items) == 2
    if kind == "anchor":
        assert _grants(items[0]) == []
        assert items[0].legacy_reason is None
        assert "legacy_reason is ignored" in caplog.text
        return
    assert _grants(items[0])[0]["selector"] == "outputs"

    legacy_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "legacy" in r.getMessage().lower()
    ]
    assert legacy_warnings == []

    legacy_infos = [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.INFO and "legacy" in r.getMessage().lower()
    ]
    assert len(legacy_infos) == 1
    assert _name(items[0]) in legacy_infos[0]
    assert reason in legacy_infos[0]


@pytest.mark.parametrize("kind", ["schedule", "trigger", "anchor"])
def test_missing_legacy_reason_keeps_warning(tmp_path, kind, caplog):
    extra = 'scope_grants = [{ resource = "path", action = "write", selector = "outputs" }]\n'
    path = _write_entry(tmp_path, kind, extra)
    with caplog.at_level(logging.INFO, logger="loom.autonomy"):
        items = _load(path, kind)
    assert len(items) == 2
    if kind == "anchor":
        assert _grants(items[0]) == []
        assert not caplog.records
        return
    assert any(
        r.levelno == logging.WARNING
        and "legacy scope_grants without skills" in r.getMessage()
        and _name(items[0]) in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize("kind", ["schedule", "trigger", "anchor"])
@pytest.mark.parametrize(
    "bad_line",
    [
        'legacy_reason = ""\n',
        'legacy_reason = "   \t  "\n',
        "legacy_reason = 42\n",
        "legacy_reason = true\n",
        'legacy_reason = ["not", "a", "string"]\n',
        'legacy_reason = { text = "nope" }\n',
    ],
)
def test_invalid_legacy_reason_rejects_entry_with_error(tmp_path, kind, bad_line, caplog):
    extra = (
        bad_line
        + 'scope_grants = [{ resource = "path", action = "write", selector = "outputs" }]\n'
    )
    path = _write_entry(tmp_path, kind, extra)
    with caplog.at_level(logging.ERROR, logger="loom.autonomy"):
        items = _load(path, kind)
    if kind == "anchor":
        assert [_name(i) for i in items] == ["curiosity", "healthy"]
        assert _grants(items[0]) == []
        assert not caplog.records
        return
    assert [_name(i) for i in items] == ["healthy"]
    assert any(
        r.levelno == logging.ERROR and "legacy_reason" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize("kind", ["schedule", "trigger", "anchor"])
def test_skills_and_legacy_reason_together_rejected_with_error(tmp_path, kind, caplog):
    _make_skill(tmp_path, "demo")
    extra = (
        'skills = ["demo"]\n'
        'legacy_reason = "contradiction"\n'
    )
    path = _write_entry(tmp_path, kind, extra)
    with caplog.at_level(logging.ERROR, logger="loom.autonomy"):
        items = _load(path, kind)
    assert [_name(i) for i in items] == ["healthy"]
    assert any(
        r.levelno == logging.ERROR and "legacy_reason" in r.getMessage()
        for r in caplog.records
    )


def test_tracked_examples_demonstrate_legacy_reason(caplog):
    repo = Path(__file__).resolve().parents[1]
    sched_raw = tomllib.loads((repo / "autonomy/schedules.example.toml").read_text(encoding="utf-8"))
    weekly = next(s for s in sched_raw["schedules"] if s["name"] == "skill_weekly_review")
    assert isinstance(weekly.get("legacy_reason"), str) and weekly["legacy_reason"].strip()

    rhythm_raw = tomllib.loads(
        (repo / "autonomy/circadian/rhythm.example.toml").read_text(encoding="utf-8")
    )
    curiosity = next(a for a in rhythm_raw["anchors"] if a["name"] == "curiosity")
    assert not {"legacy_reason", "allowed_tools", "scope_grants", "skills"} & curiosity.keys()

    with caplog.at_level(logging.INFO, logger="loom.autonomy"):
        load_schedules(repo / "autonomy/schedules.example.toml", workspace=repo)
    assert not any(
        r.levelno == logging.WARNING and "skill_weekly_review" in r.getMessage()
        for r in caplog.records
    )
    assert any(
        r.levelno == logging.INFO and "skill_weekly_review" in r.getMessage()
        for r in caplog.records
    )
