"""
AdapterRegistry — public API for registering external tools with Loom.

The ``@registry.tool()`` decorator converts an async function into a
``ToolDefinition`` and registers it in one step.

Usage
-----
    adapters = AdapterRegistry()

    @adapters.tool(trust_level="safe", tags=["http"])
    async def fetch_url(call: ToolCall) -> ToolResult:
        \"\"\"Fetch the contents of a URL.\"\"\"
        url = call.args.get("url", "")
        ...

    # Install all adapter tools into a session
    adapters.install_into(session.registry)
"""

from __future__ import annotations

from typing import Callable

from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry


class AdapterRegistry:
    """
    Holds externally registered ToolDefinitions.

    Designed for third-party tools registered through the ``@loom.tool``
    decorator. Use ``install_into()`` to merge its tools into a live
    session's ToolRegistry.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    # ------------------------------------------------------------------
    # Manual registration
    # ------------------------------------------------------------------

    def register(self, tool_def: ToolDefinition) -> None:
        """Register a ToolDefinition directly."""
        self._tools[tool_def.name] = tool_def

    def get(self, name: str) -> ToolDefinition | None:
        """Return the tool with the given name, or None."""
        return self._tools.get(name)

    def all(self) -> list[ToolDefinition]:
        """Return all registered ToolDefinitions."""
        return list(self._tools.values())

    def install_into(self, registry: ToolRegistry) -> int:
        """
        Copy all registered tools into a ToolRegistry.
        Returns the number of tools installed.
        """
        for tool in self._tools.values():
            registry.register(tool)
        return len(self._tools)

    # ------------------------------------------------------------------
    # Decorator API
    # ------------------------------------------------------------------

    def tool(
        self,
        *,
        description: str | None = None,
        trust_level: str | TrustLevel = "safe",
        input_schema: dict | None = None,
        tags: list[str] | None = None,
    ) -> Callable[[Callable], ToolDefinition]:
        """
        Decorator factory that converts an async function into a
        ToolDefinition and registers it in this AdapterRegistry.

        Parameters
        ----------
        description:  Defaults to the function's docstring if not provided.
        trust_level:  "safe" | "guarded" | "critical" (or TrustLevel enum).
        input_schema: JSON Schema dict for the tool's arguments.
        tags:         List of tag strings for filtering.

        Returns
        -------
        The created ToolDefinition (replaces the original function in the
        namespace; call ``tool_def.executor(call)`` to test the function).
        """
        def decorator(fn: Callable) -> ToolDefinition:
            desc = description or (fn.__doc__ or "").strip() or fn.__name__

            tl: TrustLevel = (
                TrustLevel[trust_level.upper()]
                if isinstance(trust_level, str)
                else trust_level
            )

            tool_def = ToolDefinition(
                name=fn.__name__,
                description=desc,
                trust_level=tl,
                input_schema=input_schema or {"type": "object", "properties": {}},
                executor=fn,
                tags=list(tags or []),
            )
            self.register(tool_def)
            return tool_def

        return decorator
