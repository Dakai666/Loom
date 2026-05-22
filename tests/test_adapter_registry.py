"""
Tests for AdapterRegistry — the @loom.tool decorator backend.

Coverage
--------
AdapterRegistry
  - register() then get()
  - get() returns None for missing
  - all() returns all registered
  - install_into() copies tools to ToolRegistry
  - @registry.tool uses function name
  - @registry.tool uses docstring as description
  - @registry.tool respects custom description
  - @registry.tool sets trust_level correctly (string + enum)
  - @registry.tool registers into registry
"""

from __future__ import annotations

from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.extensibility.adapter import AdapterRegistry


class TestAdapterRegistry:
    def _make_tool_def(self, name="my_tool"):
        async def _fn(call: ToolCall) -> ToolResult:
            return ToolResult(call_id=call.id, tool_name=name, success=True, output="ok")

        return ToolDefinition(
            name=name,
            description="A test tool",
            trust_level=TrustLevel.SAFE,
            input_schema={"type": "object", "properties": {}},
            executor=_fn,
        )

    def test_register_and_get(self):
        reg = AdapterRegistry()
        td = self._make_tool_def()
        reg.register(td)
        assert reg.get("my_tool") is td

    def test_get_unknown_returns_none(self):
        assert AdapterRegistry().get("ghost") is None

    def test_all_returns_all_registered(self):
        reg = AdapterRegistry()
        reg.register(self._make_tool_def("a"))
        reg.register(self._make_tool_def("b"))
        assert len(reg.all()) == 2

    def test_install_into_copies_to_registry(self):
        adapter_reg = AdapterRegistry()
        adapter_reg.register(self._make_tool_def("tool_x"))

        tool_reg = ToolRegistry()
        count = adapter_reg.install_into(tool_reg)
        assert count == 1
        assert tool_reg.get("tool_x") is not None

    def test_decorator_uses_function_name(self):
        reg = AdapterRegistry()

        @reg.tool()
        async def my_cool_tool(call: ToolCall) -> ToolResult: ...

        assert reg.get("my_cool_tool") is not None

    def test_decorator_uses_docstring(self):
        reg = AdapterRegistry()

        @reg.tool()
        async def documented_tool(call: ToolCall) -> ToolResult:
            """This is the docstring."""

        td = reg.get("documented_tool")
        assert "docstring" in td.description

    def test_decorator_respects_custom_description(self):
        reg = AdapterRegistry()

        @reg.tool(description="custom desc")
        async def custom_tool(call: ToolCall) -> ToolResult:
            """Should not be used."""

        assert reg.get("custom_tool").description == "custom desc"

    def test_decorator_trust_level_string(self):
        reg = AdapterRegistry()

        @reg.tool(trust_level="guarded")
        async def guarded_tool(call: ToolCall) -> ToolResult: ...

        assert reg.get("guarded_tool").trust_level == TrustLevel.GUARDED

    def test_decorator_trust_level_enum(self):
        reg = AdapterRegistry()

        @reg.tool(trust_level=TrustLevel.CRITICAL)
        async def critical_tool(call: ToolCall) -> ToolResult: ...

        assert reg.get("critical_tool").trust_level == TrustLevel.CRITICAL

    def test_decorator_registers_into_registry(self):
        reg = AdapterRegistry()

        @reg.tool()
        async def auto_registered(call: ToolCall) -> ToolResult: ...

        assert "auto_registered" in [t.name for t in reg.all()]
