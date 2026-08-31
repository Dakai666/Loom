"""
Tests for SchemaValidationMiddleware — argument coercion + error legibility.

Issue #565 (post-mortem): on 2026-08-28 the agent called ``weave_revise``
with ``changes`` serialised as a JSON *string* instead of a real array.
The middleware refused to coerce and echoed the whole 4KB payload back as
``Argument type mismatch: '[{...}]' is not of type 'array'``. The agent
never called that tool again — one illegible error permanently pushed it
off the correct path. Both halves are fixed here:

  1. JSON-text → array/object is a safe, reversible coercion (the parsed
     value must actually *be* the expected type, otherwise we leave it
     alone and let validation fail honestly).
  2. When validation does fail, the message names the path, the expected
     type and the received type — and never echoes an unbounded instance.
"""

from __future__ import annotations

import json

import pytest

from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.harness.validation import SchemaValidationMiddleware

_CHANGES_SCHEMA = {
    "type": "object",
    "properties": {
        "rationale": {"type": "string"},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": ["section", "action"],
            },
        },
        "options": {"type": "object"},
        "count": {"type": "integer"},
    },
    "required": ["rationale", "changes"],
}


async def _echo(call: ToolCall) -> ToolResult:
    return ToolResult(
        call_id=call.id, tool_name=call.tool_name, success=True,
        output=json.dumps(call.args, ensure_ascii=False, sort_keys=True),
    )


@pytest.fixture
def middleware() -> SchemaValidationMiddleware:
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="weave_revise",
        description="test double",
        trust_level=TrustLevel.SAFE,
        input_schema=_CHANGES_SCHEMA,
        executor=_echo,
    ))
    return SchemaValidationMiddleware(registry)


def _call(args: dict) -> ToolCall:
    return ToolCall(
        tool_name="weave_revise", args=args,
        trust_level=TrustLevel.SAFE, session_id="test",
    )


class TestJsonTextCoercion:
    async def test_json_string_becomes_array(self, middleware):
        """The exact 8/28 failure: `changes` arrives as JSON text."""
        changes = [{"section": "dawn", "action": "add"}]
        call = _call({"rationale": "why", "changes": json.dumps(changes)})

        result = await middleware.process(call, _echo)

        assert result.success, result.error
        assert call.args["changes"] == changes

    async def test_json_string_becomes_object(self, middleware):
        call = _call({
            "rationale": "why",
            "changes": [],
            "options": '{"dry_run": true}',
        })

        result = await middleware.process(call, _echo)

        assert result.success, result.error
        assert call.args["options"] == {"dry_run": True}

    async def test_parsed_value_of_wrong_type_is_not_coerced(self, middleware):
        """`"123"` parses as JSON, but to an int — that is not an array.

        Coercion must only fire when the parsed value *is* the expected
        type; otherwise we would smuggle a nonsense value past validation.
        """
        result = await middleware.process(
            _call({"rationale": "why", "changes": "123"}), _echo
        )
        assert not result.success

    async def test_non_json_string_is_not_coerced(self, middleware):
        result = await middleware.process(
            _call({"rationale": "why", "changes": "dawn, pet"}), _echo
        )
        assert not result.success

    async def test_real_array_is_untouched(self, middleware):
        changes = [{"section": "dawn", "action": "add"}]
        call = _call({"rationale": "why", "changes": changes})

        result = await middleware.process(call, _echo)

        assert result.success, result.error
        assert call.args["changes"] is changes


class TestErrorLegibility:
    async def test_error_names_path_expected_and_received_type(self, middleware):
        result = await middleware.process(
            _call({"rationale": "why", "changes": {"section": "dawn"}}), _echo
        )

        assert not result.success
        assert result.failure_type == "validation_error"
        assert "changes" in result.error
        assert "array" in result.error
        assert "object" in result.error

    async def test_error_does_not_echo_unbounded_instance(self, middleware):
        """A 4KB payload must not come back verbatim (the 8/28 symptom)."""
        huge = "x" * 4000
        result = await middleware.process(
            _call({"rationale": "why", "changes": huge}), _echo
        )

        assert not result.success
        assert huge not in result.error
        assert len(result.error) < 600

    async def test_unparseable_json_text_gets_a_hint(self, middleware):
        """Looks like JSON but is malformed — say so, don't just type-shame."""
        result = await middleware.process(
            _call({"rationale": "why", "changes": '[{"section": "dawn",]'}), _echo
        )

        assert not result.success
        assert "JSON" in result.error

    async def test_missing_required_field_still_reports_clearly(self, middleware):
        result = await middleware.process(_call({"changes": []}), _echo)

        assert not result.success
        assert "rationale" in result.error


class TestExistingCoercionsPreserved:
    async def test_int_from_string(self, middleware):
        call = _call({"rationale": "why", "changes": [], "count": "3"})
        result = await middleware.process(call, _echo)
        assert result.success, result.error
        assert call.args["count"] == 3
