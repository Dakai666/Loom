"""
Tests for loom.core.harness.registry — ToolDefinition and ToolRegistry.

Covers Phase 1 fragility fix from issue #347.
"""

from __future__ import annotations

import pytest

from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.harness.permissions import TrustLevel, ToolCapability


class TestToolDefinition:
    async def _noop(self, _call):
        from loom.core.harness.middleware import ToolResult
        return ToolResult(success=True, output="ok")

    def test_minimal_definition(self):
        td = ToolDefinition(
            name="noop", description="does nothing",
            trust_level=TrustLevel.SAFE,
            input_schema={"type": "object", "properties": {}},
            executor=self._noop,
        )
        assert td.name == "noop"
        assert td.trust_level == TrustLevel.SAFE

    def test_default_tags_empty(self):
        td = ToolDefinition(
            name="noop", description="x", trust_level=TrustLevel.SAFE,
            input_schema={}, executor=self._noop,
        )
        assert td.tags == []

    def test_default_capabilities_none(self):
        td = ToolDefinition(
            name="noop", description="x", trust_level=TrustLevel.SAFE,
            input_schema={}, executor=self._noop,
        )
        assert td.capabilities == ToolCapability.NONE

    def test_default_preconditions_empty(self):
        td = ToolDefinition(
            name="noop", description="x", trust_level=TrustLevel.SAFE,
            input_schema={}, executor=self._noop,
        )
        assert td.preconditions == []
        assert td.precondition_checks == []

    def test_default_inline_only_false(self):
        td = ToolDefinition(
            name="noop", description="x", trust_level=TrustLevel.SAFE,
            input_schema={}, executor=self._noop,
        )
        assert td.inline_only is False

    def test_default_spill_threshold_none(self):
        td = ToolDefinition(
            name="noop", description="x", trust_level=TrustLevel.SAFE,
            input_schema={}, executor=self._noop,
        )
        assert td.spill_threshold_chars is None

    def test_to_anthropic_schema(self):
        td = ToolDefinition(
            name="read_file", description="reads a file",
            trust_level=TrustLevel.SAFE,
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            executor=self._noop,
        )
        s = td.to_anthropic_schema()
        assert s["name"] == "read_file"
        assert "input_schema" in s
        assert s["input_schema"]["type"] == "object"

    def test_to_openai_schema(self):
        td = ToolDefinition(
            name="read_file", description="reads a file",
            trust_level=TrustLevel.SAFE,
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            executor=self._noop,
        )
        s = td.to_openai_schema()
        assert s["name"] == "read_file"
        assert "parameters" in s

    def test_tags_preserved(self):
        td = ToolDefinition(
            name="noop", description="x", trust_level=TrustLevel.SAFE,
            input_schema={}, executor=self._noop,
            tags=["io", "filesystem"],
        )
        assert td.tags == ["io", "filesystem"]

    def test_capabilities_explicit(self):
        td = ToolDefinition(
            name="exec_cmd", description="x", trust_level=TrustLevel.GUARDED,
            input_schema={}, executor=self._noop,
            capabilities=ToolCapability.EXEC | ToolCapability.MUTATES,
        )
        assert td.capabilities & ToolCapability.EXEC
        assert td.capabilities & ToolCapability.MUTATES


class TestToolRegistry:
    async def _noop(self, _call):
        from loom.core.harness.middleware import ToolResult
        return ToolResult(success=True, output="ok")

    def _def(self, name):
        return ToolDefinition(
            name=name, description=f"tool {name}",
            trust_level=TrustLevel.SAFE, input_schema={},
            executor=self._noop,
        )

    def test_empty_registry(self):
        reg = ToolRegistry()
        assert reg.list() == []
        assert reg.get("anything") is None

    def test_register_and_get(self):
        reg = ToolRegistry()
        td = self._def("my_tool")
        reg.register(td)
        assert reg.get("my_tool") is td

    def test_register_multiple(self):
        reg = ToolRegistry()
        reg.register(self._def("a"))
        reg.register(self._def("b"))
        reg.register(self._def("c"))
        assert len(reg.list()) == 3

    def test_register_duplicate_overwrites(self):
        reg = ToolRegistry()
        td1 = self._def("x")
        td2 = self._def("x")
        reg.register(td1)
        reg.register(td2)
        assert reg.get("x") is td2

    def test_get_missing_returns_none(self):
        reg = ToolRegistry()
        reg.register(self._def("exists"))
        assert reg.get("nonexistent") is None

    def test_to_anthropic_schema(self):
        reg = ToolRegistry()
        reg.register(ToolDefinition(
            name="read_file", description="reads a file",
            trust_level=TrustLevel.SAFE,
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            executor=self._noop,
        ))
        schemas = reg.to_anthropic_schema()
        assert len(schemas) == 1
        assert schemas[0]["name"] == "read_file"

    def test_to_openai_schema(self):
        reg = ToolRegistry()
        reg.register(ToolDefinition(
            name="read_file", description="reads a file",
            trust_level=TrustLevel.SAFE,
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            executor=self._noop,
        ))
        schemas = reg.to_openai_schema()
        assert len(schemas) == 1
        assert schemas[0]["name"] == "read_file"

    def test_list_returns_all(self):
        reg = ToolRegistry()
        names = {"a", "b", "c"}
        for n in names:
            reg.register(self._def(n))
        result = {td.name for td in reg.list()}
        assert result == names

    def test_list_returns_copy_not_reference(self):
        reg = ToolRegistry()
        reg.register(self._def("a"))
        lst = reg.list()
        lst.clear()
        assert reg.get("a") is not None

    def test_valid_names(self):
        reg = ToolRegistry()
        for name in ["my_tool", "tool-123", "AbcXyz", "tool__name"]:
            reg.register(self._def(name))
            assert reg.get(name) is not None

    def test_invalid_name_colon(self):
        reg = ToolRegistry()
        with pytest.raises(ValueError, match="Invalid tool name"):
            reg.register(self._def("mcp:tool"))

    def test_invalid_name_slash(self):
        reg = ToolRegistry()
        with pytest.raises(ValueError, match="Invalid tool name"):
            reg.register(self._def("tool/path"))

    def test_invalid_name_dot(self):
        reg = ToolRegistry()
        with pytest.raises(ValueError, match="Invalid tool name"):
            reg.register(self._def("tool.name"))

    def test_invalid_name_empty(self):
        reg = ToolRegistry()
        with pytest.raises(ValueError, match="Invalid tool name"):
            reg.register(self._def(""))
