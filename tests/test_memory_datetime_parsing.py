"""
Tests for ``_parse_memory_datetime`` — issue #388.

The parser must interpret naked dates / datetimes in the user's
configured timezone so that ``since``/``until`` arguments line up with
the timestamp prefix the agent sees in prompts. Strings with an explicit
offset are honored as written; the return value is always normalised
to UTC for direct comparison against DB rows.
"""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime, timedelta

import pytest

import loom.core.timezone as _tz_module
from loom.core.memory.search import MemorySearchResult
from loom.platform.cli.tools import _parse_memory_datetime


@pytest.fixture
def taipei_zone(monkeypatch):
    """Pin user zone to Asia/Taipei for a single test."""
    monkeypatch.setattr(
        _tz_module, "_USER_ZONE", zoneinfo.ZoneInfo("Asia/Taipei")
    )


def test_empty_input_returns_none():
    assert _parse_memory_datetime(None) is None
    assert _parse_memory_datetime("") is None
    assert _parse_memory_datetime("   ") is None


def test_naked_date_uses_user_zone(taipei_zone):
    # 2026-05-17 00:00 Asia/Taipei == 2026-05-16 16:00 UTC.
    result = _parse_memory_datetime("2026-05-17")
    assert result == datetime(2026, 5, 16, 16, 0, tzinfo=UTC)


def test_naked_datetime_uses_user_zone(taipei_zone):
    # 2026-05-17 16:00 Asia/Taipei == 2026-05-17 08:00 UTC.
    result = _parse_memory_datetime("2026-05-17T16:00:00")
    assert result == datetime(2026, 5, 17, 8, 0, tzinfo=UTC)


def test_explicit_offset_honored(taipei_zone):
    # Explicit +08:00 must still be respected even when user zone is Taipei.
    result = _parse_memory_datetime("2026-05-17T16:00:00+08:00")
    assert result == datetime(2026, 5, 17, 8, 0, tzinfo=UTC)


def test_explicit_offset_different_from_user_zone(taipei_zone):
    # +00:00 wins over Taipei default.
    result = _parse_memory_datetime("2026-05-17T16:00:00+00:00")
    assert result == datetime(2026, 5, 17, 16, 0, tzinfo=UTC)


def test_z_suffix_treated_as_utc(taipei_zone):
    result = _parse_memory_datetime("2026-05-17T16:00:00Z")
    assert result == datetime(2026, 5, 17, 16, 0, tzinfo=UTC)


def test_naked_date_with_utc_user_zone():
    # conftest pins user zone to UTC; naked date stays UTC.
    result = _parse_memory_datetime("2026-05-17")
    assert result == datetime(2026, 5, 17, 0, 0, tzinfo=UTC)


def test_invalid_string_raises_valueerror():
    with pytest.raises(ValueError, match="Invalid datetime"):
        _parse_memory_datetime("not-a-date")


def test_naked_datetime_with_microseconds_under_user_zone(taipei_zone):
    # Microsecond precision survives the zone conversion.
    result = _parse_memory_datetime("2026-05-17T16:00:00.123456")
    expected = datetime(2026, 5, 17, 16, 0, 0, 123456, tzinfo=zoneinfo.ZoneInfo("Asia/Taipei")).astimezone(UTC)
    assert result == expected


def test_negative_offset(taipei_zone):
    # 2026-05-17 16:00 -05:00 == 2026-05-17 21:00 UTC.
    result = _parse_memory_datetime("2026-05-17T16:00:00-05:00")
    assert result == datetime(2026, 5, 17, 21, 0, tzinfo=UTC)


def test_return_value_is_always_utc(taipei_zone):
    # Whatever the input zone, the return value's tzinfo is UTC.
    for raw in [
        "2026-05-17",
        "2026-05-17T16:00:00",
        "2026-05-17T16:00:00+08:00",
        "2026-05-17T16:00:00-05:00",
        "2026-05-17T16:00:00Z",
    ]:
        result = _parse_memory_datetime(raw)
        assert result.utcoffset() == timedelta(0), raw


# ── MemorySearchResult.format() — agent-facing recall hits ────────────


def test_recall_format_date_hint_uses_user_zone(taipei_zone):
    # Fact stored at 16:30 UTC == 00:30 next day in Asia/Taipei.
    # PR #391 review case: date hint must reflect the local date.
    result = MemorySearchResult(
        type="semantic",
        key="foo",
        value="bar",
        score=1.0,
        updated_at="2026-05-16T16:30:00+00:00",
    )
    out = result.format()
    assert " [2026-05-17] " in out


def test_recall_format_date_hint_under_utc():
    # conftest pins user zone to UTC; date hint matches stored date.
    result = MemorySearchResult(
        type="semantic",
        key="foo",
        value="bar",
        score=1.0,
        updated_at="2026-05-16T16:30:00+00:00",
    )
    out = result.format()
    assert " [2026-05-16] " in out


def test_recall_format_naked_timestamp_treated_as_utc(taipei_zone):
    # Stored strings without offset (legacy rows) are treated as UTC.
    result = MemorySearchResult(
        type="semantic",
        key="foo",
        value="bar",
        score=1.0,
        updated_at="2026-05-16T16:30:00",
    )
    out = result.format()
    assert " [2026-05-17] " in out


def test_recall_format_invalid_timestamp_falls_back_to_slice():
    result = MemorySearchResult(
        type="semantic",
        key="foo",
        value="bar",
        score=1.0,
        updated_at="not-an-iso-string",
    )
    out = result.format()
    assert " [not-an-iso" in out


def test_recall_format_empty_timestamp_omits_hint():
    result = MemorySearchResult(
        type="semantic", key="foo", value="bar", score=1.0, updated_at=""
    )
    out = result.format()
    assert "[" not in out.split("\n")[0].replace("[semantic]", "")
