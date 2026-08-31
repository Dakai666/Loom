"""
Middleware for runtime validation of Tool arguments against their JSON Schema.
"""

import json
from typing import Any

import jsonschema

from .middleware import Middleware, ToolCall, ToolResult, ToolHandler
from .registry import ToolRegistry

# Upper bound on how much of a jsonschema message we hand back to the model.
# Its default rendering embeds the offending instance verbatim, so a 4KB
# argument becomes a 4KB error (issue #565): the agent burns context on its
# own rejected payload and learns nothing about how to fix the call.
_MAX_DETAIL_CHARS = 200

# A string is worth *trying* to parse as JSON only when it opens like a
# container. Keeps us from running json.loads over every prose argument.
_JSON_OPENERS = ("[", "{")


def _json_type_name(val: Any) -> str:
    """JSON Schema's name for a Python value's type — so the error message
    speaks the same vocabulary as the schema the model was given."""
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "boolean"
    if isinstance(val, str):
        return "string"
    if isinstance(val, int):
        return "integer"
    if isinstance(val, float):
        return "number"
    if isinstance(val, list):
        return "array"
    if isinstance(val, dict):
        return "object"
    return type(val).__name__


def _looks_like_json_text(val: Any) -> bool:
    return isinstance(val, str) and val.strip()[:1] in _JSON_OPENERS


class SchemaValidationMiddleware(Middleware):
    """
    Validates tool arguments against the tool's defined JSON schema before execution.
    If the arguments are structurally incompatible, execution is short-circuited
    and a validation error is returned to the LLM.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def _coerce_args(self, args: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        """Attempt safe type coercions where possible (e.g. string to int)."""
        properties = schema.get("properties", {})
        
        coerced = dict(args)
        for key, expected_type_def in properties.items():
            if key not in coerced:
                continue
            
            val = coerced[key]
            expected_type = expected_type_def.get("type")
            if not expected_type:
                # E.g. anything allowed or missing type
                continue
                
            if expected_type == "string" and not isinstance(val, str):
                coerced[key] = str(val)
            elif expected_type == "integer" and isinstance(val, str):
                try:
                    coerced[key] = int(val)
                except ValueError:
                    pass
            elif expected_type == "number" and isinstance(val, str):
                try:
                    coerced[key] = float(val)
                except ValueError:
                    pass
            elif expected_type == "boolean" and isinstance(val, str):
                lower_val = val.lower()
                if lower_val in ("true", "1", "yes"):
                    coerced[key] = True
                elif lower_val in ("false", "0", "no"):
                    coerced[key] = False
            elif expected_type in ("array", "object") and _looks_like_json_text(val):
                # Models routinely serialise a structured argument to JSON text
                # instead of emitting a real array/object — issue #565 traced a
                # tool being abandoned outright to exactly this. Parsing it back
                # is lossless *provided* the parsed value is genuinely the type
                # the schema asked for; anything else (a bare number, a nested
                # string) is left alone so validation still fails honestly
                # rather than smuggling a wrong-shaped value through.
                try:
                    parsed = json.loads(val)
                except (ValueError, TypeError):
                    continue
                if _json_type_name(parsed) == expected_type:
                    coerced[key] = parsed
                    
        return coerced

    def _format_error(self, exc: jsonschema.ValidationError) -> str:
        """Render a validation failure the model can act on.

        Names the argument path, the expected type and what actually
        arrived — and never echoes the instance itself, which for a large
        argument is both useless and context-expensive (issue #565).
        """
        path_str = " -> ".join(str(p) for p in exc.path) if exc.path else "root"

        if exc.validator == "type":
            expected = exc.validator_value
            expected_str = (
                expected if isinstance(expected, str)
                else " or ".join(str(t) for t in expected)
            )
            got = _json_type_name(exc.instance)
            msg = (
                f"Argument type mismatch at path [{path_str}]: "
                f"expected {expected_str}, got {got}."
            )
            if got == "string" and expected_str in ("array", "object"):
                # We already tried to parse it in _coerce_args and failed, so
                # the text is malformed rather than merely stringified.
                msg += (
                    f" The value looks like JSON text but could not be parsed."
                    f" Send a real {expected_str} value, not a quoted string."
                    if _looks_like_json_text(exc.instance)
                    else f" Send a real {expected_str} value, not a string."
                )
            return msg

        detail = exc.message
        if len(detail) > _MAX_DETAIL_CHARS:
            detail = detail[:_MAX_DETAIL_CHARS] + "… (truncated)"
        return f"Argument validation failed at path [{path_str}]: {detail}"

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        tool_def = self._registry.get(call.tool_name)
        if tool_def is None:
            return await next(call)
            
        schema = tool_def.input_schema
        if not schema:
            return await next(call)

        try:
            coerced_args = self._coerce_args(call.args, schema)
            jsonschema.validate(instance=coerced_args, schema=schema)
            call.args = coerced_args
        except jsonschema.ValidationError as e:
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error=self._format_error(e),
                failure_type="validation_error",
            )
            
        return await next(call)
