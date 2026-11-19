"""Action-row output capture (#569) — ``capture_output_fields``.

Recall shows ``action_records.output_prefix``; this pins the projection.
The predict-settlement half of the old surface tests retired with #674.
"""

from loom.core.memory.observation import (
    OUTPUT_PREFIX_MAX_CHARS,
    capture_output_fields,
)


class TestCaptureOutputFields:
    def test_success_string_is_captured_whole_when_small(self):
        f = capture_output_fields("PASS: 3 tests\nok", success=True, error=None)
        assert f["output_prefix"] == "PASS: 3 tests\nok"
        assert f["output_len"] == len("PASS: 3 tests\nok")
        assert f["output_rows"] == 2
        assert len(f["output_digest"]) == 64  # sha256 hex

    def test_failure_captures_the_error_text(self):
        """On failure the error text (e.g. a 429 body) is what the agent saw."""
        f = capture_output_fields("ignored", success=False, error="HTTP 429 rate limited")
        assert f["output_prefix"] == "HTTP 429 rate limited"
        assert f["output_rows"] == 1

    def test_success_none_output_is_empty_not_the_string_None(self):
        f = capture_output_fields(None, success=True, error=None)
        assert f["output_prefix"] == ""
        assert f["output_len"] == 0
        assert f["output_rows"] == 0

    def test_list_output_rows_is_element_count(self):
        f = capture_output_fields(["r1", "r2", "r3"], success=True, error=None)
        assert f["output_rows"] == 3

    def test_long_output_prefix_capped_digest_of_full_text(self):
        big = "x" * (OUTPUT_PREFIX_MAX_CHARS + 100)
        f = capture_output_fields(big, success=True, error=None)
        assert len(f["output_prefix"]) == OUTPUT_PREFIX_MAX_CHARS
        assert f["output_len"] == len(big)
        # digest covers the FULL text — auditability of the truncated capture
        small = capture_output_fields(big[:OUTPUT_PREFIX_MAX_CHARS], success=True, error=None)
        assert f["output_digest"] != small["output_digest"]
