"""
Middleware for runtime validation of Tool arguments against their JSON Schema.
"""

from typing import Any
import jsonschema

from .middleware import Middleware, ToolCall, ToolResult, ToolHandler
from .registry import ToolRegistry


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
            elif expected_type == "array" and not isinstance(val, list):
                # We do not coerce scalar -> list. They should supply array.
                pass
                    
        return coerced

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
            path_str = " -> ".join(str(p) for p in e.path) if e.path else "root"
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error=f"Argument type mismatch: {e.message} at path [{path_str}]",
                failure_type="validation_error",
            )
            
        return await next(call)
