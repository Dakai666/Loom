"""
Event Consumer Map Completeness Tests (Issue #146)
===================================================

Verifies that:

1. Every event type exported from ``loom.core.events.__all__`` is documented
   in the Consumer Map table inside the module docstring — so the table never
   drifts out of sync when a new event is added.

2. ``EventConsumer`` protocol is importable and has the four required handler
   stubs as protocol methods.

3. ``TrustLevel`` display properties work correctly and are idempotent
   (Issue #148).

These are **static / unit-level** tests — no agent session is started.
"""

import inspect
import re
from typing import get_type_hints

import pytest

import loom.core.events as events_module
from loom.core.events import (
    EventConsumer,
    TextChunk,
    ToolBegin,
    ToolEnd,
    TurnDone,
)
from loom.core.harness.permissions import TrustLevel


# ---------------------------------------------------------------------------
# Issue #146 — Consumer Map completeness
# ---------------------------------------------------------------------------

# Events that intentionally do NOT appear in the Consumer Map table because
# they are view-model helpers, not stream events.  Update this set if new
# non-event exports are added to __all__.
_NON_EVENT_EXPORTS: frozenset[str] = frozenset(
    {
        "EventConsumer",
        "ExecutionEnvelopeView",
        "ExecutionNodeView",
        "GrantSummary",
    }
)


def _consumer_map_event_names() -> set[str]:
    """Parse the event names from the Consumer Map table in events.py docstring."""
    doc = events_module.__doc__ or ""
    # Match lines like: | TextChunk           |  ✓  |  ✓  |    ✓    |   YES    |
    # Capture the first cell (event name), stripping whitespace.
    pattern = re.compile(r"^\|\s+(\w+)\s+\|", re.MULTILINE)
    return {m.group(1) for m in pattern.finditer(doc)}


def test_all_stream_events_in_consumer_map():
    """Every event in __all__ (except view-model helpers) must appear in the Consumer Map.

    When you add a new event type to loom/core/events.py, you MUST:
      1. Add it to ``__all__``
      2. Add it to the Consumer Map table in the module docstring
      3. Add ``Producers:`` / ``Consumers:`` to its class docstring

    If this test fails, you have added a new event to ``__all__`` without
    updating the Consumer Map.  Update the table in the module docstring.
    """
    all_exports = set(events_module.__all__)
    stream_events = all_exports - _NON_EVENT_EXPORTS

    map_entries = _consumer_map_event_names()

    missing = stream_events - map_entries
    assert not missing, (
        f"The following event(s) are exported in __all__ but missing from the "
        f"Consumer Map table in loom/core/events.py:\n\n"
        + "\n".join(f"  - {name}" for name in sorted(missing))
        + "\n\nAdd each missing event to the table and add Producers:/Consumers: "
        "to its class docstring."
    )


def test_consumer_map_has_no_unknown_events():
    """The Consumer Map must not reference event names that don't exist in __all__.

    This catches typos and stale entries after event renames.
    """
    all_exports = set(events_module.__all__)
    map_entries = _consumer_map_event_names()

    unknown = map_entries - all_exports
    # Remove header row artefacts that the regex might capture
    unknown.discard("Event")
    assert not unknown, (
        f"The following name(s) appear in the Consumer Map table but are NOT "
        f"in __all__:\n\n"
        + "\n".join(f"  - {name}" for name in sorted(unknown))
        + "\n\nEither remove the stale row from the Consumer Map table or add "
        "the missing export to __all__."
    )


def test_event_consumer_protocol_is_importable():
    """EventConsumer must be importable from loom.core.events."""
    assert EventConsumer is not None


def test_event_consumer_protocol_has_required_methods():
    """EventConsumer protocol must expose the four required handler stubs."""
    required = {"on_text_chunk", "on_tool_begin", "on_tool_end", "on_turn_done"}
    protocol_methods = {
        name
        for name, member in inspect.getmembers(EventConsumer)
        if not name.startswith("_")
        and callable(getattr(EventConsumer, name, None))
    }
    missing = required - protocol_methods
    assert not missing, (
        f"EventConsumer protocol is missing required method(s): "
        f"{sorted(missing)}.  Add them to the Protocol definition."
    )


def test_event_consumer_in_all():
    """EventConsumer must be exported via __all__ so static analysers see it."""
    assert "EventConsumer" in events_module.__all__


# ---------------------------------------------------------------------------
# Issue #146 — event class docstrings have Producers / Consumers sections
# ---------------------------------------------------------------------------

# Required events must document both sections; optional events should too
# but we only enforce it on the four required ones.
_REQUIRED_EVENTS = (TextChunk, ToolBegin, ToolEnd, TurnDone)


@pytest.mark.parametrize("event_cls", _REQUIRED_EVENTS, ids=lambda c: c.__name__)
def test_required_event_has_producers_section(event_cls):
    """Required events must document their Producers in the class docstring."""
    doc = event_cls.__doc__ or ""
    assert "Producers:" in doc, (
        f"{event_cls.__name__} docstring is missing a 'Producers:' section. "
        "Add it to describe which session code yields this event."
    )


@pytest.mark.parametrize("event_cls", _REQUIRED_EVENTS, ids=lambda c: c.__name__)
def test_required_event_has_consumers_section(event_cls):
    """Required events must document their Consumers in the class docstring."""
    doc = event_cls.__doc__ or ""
    assert "Consumers:" in doc, (
        f"{event_cls.__name__} docstring is missing a 'Consumers:' section. "
        "Add it to describe which platforms handle this event."
    )


# ---------------------------------------------------------------------------
# Issue #148 — TrustLevel display properties
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("level", list(TrustLevel))
def test_trust_level_plain_is_uppercase(level):
    """TrustLevel.plain must return uppercase ASCII with no markup."""
    result = level.plain
    assert result == result.upper(), f"{level}.plain is not uppercase: {result!r}"
    assert "[" not in result, (
        f"{level}.plain must not contain Rich markup, got: {result!r}"
    )


@pytest.mark.parametrize("level", list(TrustLevel))
def test_trust_level_display_plain_equals_plain(level):
    """display_plain must be identical to plain (alias contract)."""
    assert level.display_plain == level.plain, (
        f"{level}.display_plain != {level}.plain — alias is broken"
    )


@pytest.mark.parametrize("level", list(TrustLevel))
def test_trust_level_label_contains_markup(level):
    """TrustLevel.label must contain Rich markup tags."""
    result = level.label
    assert "[" in result and "]" in result, (
        f"{level}.label should contain Rich markup, got: {result!r}"
    )


@pytest.mark.parametrize("level", list(TrustLevel))
def test_trust_level_display_rich_equals_label(level):
    """display_rich must be identical to label (alias contract)."""
    assert level.display_rich == level.label, (
        f"{level}.display_rich != {level}.label — alias is broken"
    )


@pytest.mark.parametrize("level", list(TrustLevel))
def test_trust_level_plain_not_equal_label(level):
    """plain and label must differ — plain must never contain Rich markup."""
    assert level.plain != level.label, (
        f"{level}.plain and .label are identical — likely a broken alias"
    )


def test_trust_level_plain_values():
    """Spot-check the exact plain string values."""
    assert TrustLevel.SAFE.plain == "SAFE"
    assert TrustLevel.GUARDED.plain == "GUARDED"
    assert TrustLevel.CRITICAL.plain == "CRITICAL"


def test_trust_level_display_plain_values():
    """display_plain values must match plain exactly."""
    assert TrustLevel.SAFE.display_plain == "SAFE"
    assert TrustLevel.GUARDED.display_plain == "GUARDED"
    assert TrustLevel.CRITICAL.display_plain == "CRITICAL"
