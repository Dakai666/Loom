"""
Tests for the shared autonomy permission-field parser (issue #525).

``trust_level`` / ``allowed_tools`` / ``scope_grants`` are the three fields a
``schedules.toml`` entry already uses to pre-authorise an autonomous turn. The
parser is extracted so the circadian rhythm table can declare the *same* fields
through the *same* code path (DK: "純 code 層統一") — a rhythm anchor and a cron
schedule now describe their permissions identically.

The contract is tolerant, mirroring the rest of the autonomy loaders: a missing
field yields a neutral default, a malformed one is dropped (not raised) so a
single typo never silences the whole entry.
"""

from __future__ import annotations

from loom.autonomy.permission_fields import (
    VALID_TRUST_LEVELS,
    parse_permission_fields,
    validate_trust_level,
)


class TestValidateTrustLevel:
    def test_valid_levels_pass_through(self):
        for level in VALID_TRUST_LEVELS:
            assert validate_trust_level(level, "x") == level

    def test_invalid_defaults_to_guarded(self):
        assert validate_trust_level("paranoid", "sched") == "guarded"

    def test_empty_defaults_to_guarded(self):
        assert validate_trust_level("", "sched") == "guarded"


class TestParsePermissionFields:
    def test_empty_dict_yields_neutral_defaults(self):
        out = parse_permission_fields({}, "x")
        assert out == {"trust_level": None, "allowed_tools": [], "scope_grants": []}

    def test_missing_trust_level_is_none_not_guarded(self):
        # A bare anchor declares no trust override → None, so the caller keeps
        # its own default (anchors want "no override", schedules want "guarded").
        assert parse_permission_fields({"allowed_tools": ["x"]}, "p")["trust_level"] is None

    def test_present_trust_level_validated(self):
        assert parse_permission_fields({"trust_level": "safe"}, "p")["trust_level"] == "safe"

    def test_invalid_trust_level_validated_to_guarded(self):
        assert parse_permission_fields({"trust_level": "nope"}, "p")["trust_level"] == "guarded"

    def test_allowed_tools_coerced_to_str_list(self):
        out = parse_permission_fields({"allowed_tools": ["run_bash", "write_file"]}, "p")
        assert out["allowed_tools"] == ["run_bash", "write_file"]

    def test_allowed_tools_non_list_dropped(self):
        assert parse_permission_fields({"allowed_tools": "run_bash"}, "p")["allowed_tools"] == []

    def test_scope_grants_keep_well_formed_entries(self):
        grants = [
            {"resource": "path", "action": "write", "selector": "news"},
            {"resource": "mutation", "action": "mutate"},  # selector optional
        ]
        out = parse_permission_fields({"scope_grants": grants}, "p")
        assert out["scope_grants"] == grants

    def test_scope_grants_drop_malformed_entries(self):
        # An entry missing resource/action, or not a dict, is dropped — not raised.
        grants = [
            {"resource": "path", "action": "write"},
            {"resource": "path"},          # no action
            "not-a-dict",                  # wrong type
        ]
        out = parse_permission_fields({"scope_grants": grants}, "p")
        assert out["scope_grants"] == [{"resource": "path", "action": "write"}]

    def test_scope_grants_non_list_dropped(self):
        assert parse_permission_fields({"scope_grants": {"resource": "x"}}, "p")["scope_grants"] == []
