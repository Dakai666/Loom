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

    def test_time_list_expands_to_one_anchor_per_slot(self, tmp_path):
        # Issue #526: one activity, many time slots. ``time`` accepts a list;
        # name is the *item identity* (the join key into daily_weave), not the
        # trigger key. Both slots share name + meaning, fire independently.
        p = tmp_path / "multi.toml"
        _write(p, '''
            [[anchors]]
            time = ["10:00", "19:00"]
            name = "pet"
            meaning = "喵吉照顧"
        ''')
        anchors = load_rhythm(p)
        assert [a.name for a in anchors] == ["pet", "pet"]
        assert [a.time for a in anchors] == ["10:00", "19:00"]
        assert [a.meaning for a in anchors] == ["喵吉照顧", "喵吉照顧"]
        # Trigger names must be unique per slot or the second silently collides
        # (the original #526 bug). Item identity (name) stays shared for weave.
        assert [a.trigger_name for a in anchors] == [
            "circadian:phase_pet@1000",
            "circadian:phase_pet@1900",
        ]

    def test_single_element_time_list_keeps_plain_trigger(self, tmp_path):
        # A one-element list is indistinguishable from a scalar time: no slot
        # suffix, so the trigger name stays backward-compatible.
        p = tmp_path / "one.toml"
        _write(p, '''
            [[anchors]]
            time = ["09:00"]
            name = "dawn"
        ''')
        anchors = load_rhythm(p)
        assert len(anchors) == 1
        assert anchors[0].trigger_name == "circadian:phase_dawn"

    def test_scalar_time_unchanged(self, tmp_path):
        # Backward compat: the overwhelming common case (scalar time) is one
        # anchor with the plain, unsuffixed trigger name.
        p = tmp_path / "scalar.toml"
        _write(p, '''
            [[anchors]]
            time = "08:00"
            name = "dawn"
        ''')
        anchors = load_rhythm(p)
        assert len(anchors) == 1
        assert anchors[0].time == "08:00"
        assert anchors[0].trigger_name == "circadian:phase_dawn"

    def test_time_list_drops_invalid_slots_keeps_valid(self, tmp_path):
        # Tolerant by contract (#460): a bad slot inside the list is dropped
        # individually, the valid ones survive. With only one valid slot left,
        # the suffix disappears (the trigger name reflects reality, not intent).
        p = tmp_path / "partial_list.toml"
        _write(p, '''
            [[anchors]]
            time = ["10:00", "25:00"]
            name = "pet"
        ''')
        anchors = load_rhythm(p)
        assert [a.time for a in anchors] == ["10:00"]
        assert anchors[0].trigger_name == "circadian:phase_pet"

    def test_time_list_all_invalid_drops_block(self, tmp_path):
        p = tmp_path / "all_bad.toml"
        _write(p, '''
            [[anchors]]
            time = ["25:00", "99:99"]
            name = "pet"

            [[anchors]]
            time = "08:00"
            name = "dawn"
        ''')
        anchors = load_rhythm(p)
        assert [a.name for a in anchors] == ["dawn"]

    def test_empty_time_list_drops_block(self, tmp_path):
        p = tmp_path / "empty_list.toml"
        _write(p, '''
            [[anchors]]
            time = []
            name = "pet"

            [[anchors]]
            time = "08:00"
            name = "dawn"
        ''')
        anchors = load_rhythm(p)
        assert [a.name for a in anchors] == ["dawn"]

    def test_duplicate_block_name_still_keeps_first(self, tmp_path):
        # name uniqueness is still an invariant *across blocks* — multi-time is
        # expressed via the list, so a duplicate block name is a genuine error
        # (keep-first, same as before). Two pet *blocks* is wrong; one pet block
        # with two times is right.
        p = tmp_path / "dup_block.toml"
        _write(p, '''
            [[anchors]]
            time = "10:00"
            name = "pet"
            meaning = "first"

            [[anchors]]
            time = "19:00"
            name = "pet"
            meaning = "second — duplicate block, ignored"
        ''')
        anchors = load_rhythm(p)
        assert [a.name for a in anchors] == ["pet"]
        assert anchors[0].meaning == "first"

    def test_anchor_carries_permission_fields(self, tmp_path):
        # Issue #525: an anchor can declare the same trust_level / allowed_tools
        # / scope_grants a schedules.toml entry uses, so a circadian phase can
        # do routine tool work (research, write a draft, run the pet script)
        # without re-asking DK every day. The fields ride through to the chime.
        p = tmp_path / "perm.toml"
        _write(p, '''
            [[anchors]]
            time = "11:00"
            name = "curiosity"
            allowed_tools = ["fetch_url", "web_search", "write_file"]
            scope_grants = [
              { resource = "path", action = "write", selector = "autonomy/circadian" },
            ]
        ''')
        a = load_rhythm(p)[0]
        assert a.allowed_tools == ("fetch_url", "web_search", "write_file")
        assert a.scope_grants == (
            {"resource": "path", "action": "write", "selector": "autonomy/circadian"},
        )
        assert a.trust_level is None  # no override declared

    def test_anchor_without_permission_fields_defaults_empty(self, tmp_path):
        p = tmp_path / "plain.toml"
        _write(p, '''
            [[anchors]]
            time = "09:00"
            name = "dawn"
        ''')
        a = load_rhythm(p)[0]
        assert a.allowed_tools == ()
        assert a.scope_grants == ()
        assert a.trust_level is None

    def test_multi_time_anchor_shares_permission_fields(self, tmp_path):
        # The permission fields are an attribute of the activity identity, so
        # every expanded slot of a recurring activity carries the same grants.
        p = tmp_path / "petperm.toml"
        _write(p, '''
            [[anchors]]
            time = ["10:00", "19:00"]
            name = "pet"
            trust_level = "safe"
            allowed_tools = ["run_bash"]
        ''')
        anchors = load_rhythm(p)
        assert len(anchors) == 2
        for a in anchors:
            assert a.trust_level == "safe"
            assert a.allowed_tools == ("run_bash",)

    def test_anchor_invalid_trust_level_falls_back_guarded(self, tmp_path):
        p = tmp_path / "badtrust.toml"
        _write(p, '''
            [[anchors]]
            time = "09:00"
            name = "dawn"
            trust_level = "paranoid"
        ''')
        assert load_rhythm(p)[0].trust_level == "guarded"

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
