"""
Tests for the rhythm-table reader (issue #460).

The reader is the data feed for circadian phase anchors: per-agent file,
TOML format, tolerant of every kind of mess so that a missing or broken
table never kills the dawn/close lifecycle that PR1 already runs.
"""

from __future__ import annotations

from pathlib import Path

from loom.autonomy.circadian.rhythm import Anchor, load_rhythm


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class TestLoadRhythm:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_rhythm(tmp_path / "missing.toml") == []

    def test_parses_full_anchor_set(self, tmp_path):
        p = tmp_path / "rhythm.toml"
        _write(p, '''
            [[anchors]]
            time = "08:00"
            name = "dawn"
            public = true
            meaning = "醒來成為今天的絲絲"

            [[anchors]]
            time = "09:00"
            name = "shared_learning"
            meaning = "和 DK 一起吸收世界"

            [[anchors]]
            time = "23:00"
            name = "evening_closure"
            public = false
            meaning = """
人際收束 + 收織 + 安排明天
"""
        ''')
        anchors = load_rhythm(p)
        assert [a.name for a in anchors] == ["dawn", "shared_learning", "evening_closure"]
        # public defaults to True; explicit false respected
        assert [a.public for a in anchors] == [True, True, False]
        # meaning is stripped (no leading/trailing whitespace, but inner kept)
        assert anchors[2].meaning == "人際收束 + 收織 + 安排明天"

    def test_trigger_name_is_namespaced(self):
        a = Anchor(time="08:00", name="dawn", meaning="x")
        assert a.trigger_name == "circadian:phase_dawn"

    def test_invalid_toml_returns_empty(self, tmp_path):
        p = tmp_path / "broken.toml"
        _write(p, "[[anchors\nname = 'dawn'")
        assert load_rhythm(p) == []

    def test_missing_anchors_array_returns_empty(self, tmp_path):
        p = tmp_path / "no_anchors.toml"
        _write(p, "title = 'rhythm but no anchors'\n")
        assert load_rhythm(p) == []

    def test_anchors_not_array_returns_empty(self, tmp_path):
        p = tmp_path / "scalar.toml"
        _write(p, "anchors = 'not a list'\n")
        assert load_rhythm(p) == []

    def test_skips_anchor_missing_required_field(self, tmp_path):
        p = tmp_path / "partial.toml"
        _write(p, '''
            [[anchors]]
            time = "08:00"
            # no name

            [[anchors]]
            time = "09:00"
            name = "shared_learning"
        ''')
        anchors = load_rhythm(p)
        assert [a.name for a in anchors] == ["shared_learning"]

    def test_skips_anchor_with_invalid_time(self, tmp_path):
        p = tmp_path / "bad_time.toml"
        _write(p, '''
            [[anchors]]
            time = "25:00"
            name = "dawn"

            [[anchors]]
            time = "08:60"
            name = "shared_learning"

            [[anchors]]
            time = "no-colons"
            name = "pet"

            [[anchors]]
            time = "08:00"
            name = "dawn_ok"
        ''')
        anchors = load_rhythm(p)
        assert [a.name for a in anchors] == ["dawn_ok"]

    def test_skips_duplicate_anchor_names(self, tmp_path):
        p = tmp_path / "dupes.toml"
        _write(p, '''
            [[anchors]]
            time = "08:00"
            name = "dawn"
            meaning = "first"

            [[anchors]]
            time = "08:30"
            name = "dawn"
            meaning = "second — should be ignored"
        ''')
        anchors = load_rhythm(p)
        assert len(anchors) == 1
        assert anchors[0].meaning == "first"

    def test_meaning_defaults_to_empty_when_omitted(self, tmp_path):
        p = tmp_path / "no_meaning.toml"
        _write(p, '''
            [[anchors]]
            time = "08:00"
            name = "dawn"
        ''')
        anchors = load_rhythm(p)
        assert anchors[0].meaning == ""

    def test_per_agent_isolation_via_separate_paths(self, tmp_path):
        sisi = tmp_path / "sisi" / "rhythm.toml"
        xiaoqing = tmp_path / "xiaoqing" / "rhythm.toml"
        _write(sisi, '''
            [[anchors]]
            time = "08:00"
            name = "dawn"
            meaning = "絲絲"
        ''')
        _write(xiaoqing, '''
            [[anchors]]
            time = "09:00"
            name = "morning_ledger"
            meaning = "記帳"
        ''')
        assert load_rhythm(sisi)[0].name == "dawn"
        assert load_rhythm(xiaoqing)[0].name == "morning_ledger"
